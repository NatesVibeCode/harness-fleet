"""Air-gap boundary packet and CSV exporter."""
import csv
import json
import time
from pathlib import Path
from typing import Any

from .models import (
    ClaimFilter,
    CleanPacket,
    ExtractedItem,
    FilterClause,
    FilterOp,
    ProviderReceipt,
    RoutePolicy,
    SortSpec,
    TaskSpec,
)


def _as_int(value: Any) -> int:
    """Token counts from raw provider usage JSON; anything non-numeric is 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _values_equal(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    # Booleans are not 0/1 for filtering: a `passed: true` clause must not
    # match score 1, and `count: 0` must not match false.
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected or str(actual).lower() == str(expected).lower()


def _clause_matches(claims: dict[str, Any], clause: FilterClause) -> bool:
    key, op, expected = clause.field, clause.op, clause.value
    if op in (FilterOp.IN, FilterOp.NOT_IN):
        options = expected if isinstance(expected, list) else [expected]
        hit = key in claims and any(_values_equal(claims[key], opt) for opt in options)
        return not hit if op == FilterOp.NOT_IN else hit
    if key not in claims:
        # Fail closed: missing keys match nothing, except negations which
        # treat absence as non-membership (mirrors != on missing keys).
        return op == FilterOp.NE
    actual = claims[key]

    try:
        if op == FilterOp.EQ:
            return _values_equal(actual, expected)
        elif op == FilterOp.NE:
            if expected is None:
                return actual is not None
            if actual is None:
                return True
            return not _values_equal(actual, expected)
        elif op == FilterOp.GTE:
            return float(actual) >= float(expected)  # type: ignore[arg-type]
        elif op == FilterOp.LTE:
            return float(actual) <= float(expected)  # type: ignore[arg-type]
        elif op == FilterOp.GT:
            return float(actual) > float(expected)  # type: ignore[arg-type]
        elif op == FilterOp.LT:
            return float(actual) < float(expected)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return False


def _evaluate_filter(claims: dict[str, Any], claim_filter: ClaimFilter) -> bool:
    """Evaluate a typed ClaimFilter: ``all`` clauses must pass AND at least one
    ``any`` branch (when present) must pass.

    String comparison is case-insensitive; missing keys match nothing except
    under ``!=`` / ``NOT IN``; type errors fail closed to False.
    """
    if not all(_clause_matches(claims, clause) for clause in claim_filter.all):
        return False
    return not claim_filter.any or any(
        _evaluate_filter(claims, branch) for branch in claim_filter.any
    )


def adjust_claim_score(raw: Any, bias: float = 0.0, low: float = 0.0, high: float = 100.0) -> float | None:
    """Return bias-corrected numeric claim score, clamped to [low, high]. None if non-numeric."""
    try:
        return max(low, min(high, float(raw) - float(bias)))
    except (ValueError, TypeError):
        return None


def _build_item_route_map(run_data: dict) -> dict[str, str]:
    """Map item_id -> producing route_id via batch receipts. Best-effort; missing entries omitted."""
    mapping: dict[str, str] = {}
    for batch in (run_data.get("batches") or {}).values():
        receipt = batch.get("receipt") or {}
        route_id = receipt.get("requested_route") or receipt.get("route_id")
        if not route_id:
            continue
        result = batch.get("result")
        items: list[dict] = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict) and isinstance(result.get("items"), list):
            items = result["items"]
        for item in items:
            item_id = item.get("item_id") if isinstance(item, dict) else getattr(item, "item_id", None)
            if item_id:
                mapping[str(item_id)] = str(route_id)
    return mapping


def verified_records_from_snapshot(
    run_data: dict, *, tolerate_rejected: bool = False
) -> tuple[list[ExtractedItem], TaskSpec]:
    """Collect verified records from a run snapshot, revalidating claims.

    Shared by packet/CSV exporters and DAG filter nodes so every consumer
    sees the same record set: only verified batches, claims revalidated
    against the run's task.

    ``tolerate_rejected`` is for *readers* of a run rather than producers of a
    deliverable: a record that no longer validates — because the scoring rule
    moved on after it was written — is left out and counted instead of taking
    the whole page down with it. One legacy record made the results board refuse
    to open at all, which hides a hundred good ones to be strict about one.
    """
    task = TaskSpec.model_validate(run_data["task"])
    verified_records: list[ExtractedItem] = []
    rejected = 0
    for b in run_data.get("batches", {}).values():
        if b.get("status") == "verified" and b.get("result"):
            results = b["result"]
            if isinstance(results, list):
                validated = [ExtractedItem.model_validate(item) for item in results]
            elif isinstance(results, dict) and "items" in results:
                validated = [ExtractedItem.model_validate(item) for item in results["items"]]
            else:
                raise ValueError("verified batch result has an invalid shape")
            for item in validated:
                try:
                    task.validate_extracted_item(item)
                except ValueError:
                    if not tolerate_rejected:
                        raise
                    rejected += 1
                    continue
                verified_records.append(item)
    if tolerate_rejected:
        # Recorded on the caller's mapping so a reader can report it. The count
        # is the caller's to keep: strictness belongs to the deliverable, and a
        # page that hides what it left out has told a reader half the truth.
        run_data["records_rejected"] = rejected
    return verified_records, task


def _filter_and_sort_records(
    records: list[ExtractedItem],
    sort: SortSpec | None = None,
    top: int | None = None,
    claim_filter: ClaimFilter | None = None,
    bias_map: dict[str, float] | None = None,
    score_field: str | None = None,
    item_route_map: dict[str, str] | None = None,
) -> list[ExtractedItem]:
    """Filter/sort records. When bias_map+score_field are given and sort.field==score_field,
    ranking uses bias-adjusted scores (raw - route bias) so mixed-rater CSVs stay comparable.
    Raw claims are never mutated; only the sort key changes."""
    res = list(records)
    if claim_filter is not None:
        res = [r for r in res if _evaluate_filter(r.claims, claim_filter)]
    if sort is not None:
        sort_by, descending = sort.field, sort.descending
        def sort_key(rec: ExtractedItem):
            v = rec.claims.get(sort_by)
            if v is None:
                return (0, 0.0, "")
            try:
                numeric = float(v)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                return (1 if descending else 2, 0.0, str(v))
            if bias_map and score_field and sort_by == score_field and item_route_map:
                route_id = item_route_map.get(rec.item_id)
                if route_id and route_id in bias_map:
                    adj = adjust_claim_score(numeric, bias_map[route_id])
                    if adj is not None:
                        numeric = adj
            return (2 if descending else 1, numeric, "")
        res.sort(key=sort_key, reverse=descending)
    if top is not None:
        if top < 0:
            raise ValueError("top must be non-negative")
        res = res[:top]
    return res


def export_clean_csv(
    run_data: dict,
    output_path: Path,
    sort: SortSpec | None = None,
    top: int | None = None,
    rank: bool = False,
    claim_filter: ClaimFilter | None = None,
    bias_map: dict[str, float] | None = None,
    score_field: str | None = None,
) -> Path:
    """Project verified records into a frictionless tabular CSV format."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    verified_records, task = verified_records_from_snapshot(run_data)

    item_route_map = _build_item_route_map(run_data) if bias_map and score_field else None
    verified_records = _filter_and_sort_records(
        verified_records,
        sort=sort,
        top=top,
        claim_filter=claim_filter,
        bias_map=bias_map,
        score_field=score_field,
        item_route_map=item_route_map,
    )

    # Determine all unique claim keys, excluding reserved standard column headers
    reserved_headers = {"item_id", "primary_quote_text", "quote_count", "source_uri", "source_digest"}
    if rank:
        reserved_headers.add("rank")
    claim_keys: list[str] = []
    if task.claims_schema and "properties" in task.claims_schema:
        claim_keys = [k for k in task.claims_schema["properties"].keys() if k not in reserved_headers]
    for rec in verified_records:
        for k in rec.claims.keys():
            if k not in claim_keys and k not in reserved_headers:
                claim_keys.append(k)

    fieldnames = []
    if rank:
        fieldnames.append("rank")
    fieldnames.append("item_id")
    fieldnames.extend(claim_keys)
    fieldnames.extend(["primary_quote_text", "quote_count", "source_uri", "source_digest"])

    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for idx, rec in enumerate(verified_records, start=1):
            row = {
                "item_id": rec.item_id,
                "primary_quote_text": rec.quotes[0].text if rec.quotes else "",
                "quote_count": len(rec.quotes),
                "source_uri": rec.source_uri or "",
                "source_digest": rec.source_digest,
            }
            if rank:
                row["rank"] = idx
            for k in claim_keys:
                val = rec.claims.get(k)
                if isinstance(val, (dict, list)):
                    row[k] = json.dumps(val, ensure_ascii=False)
                elif val is not None:
                    row[k] = str(val)
                else:
                    row[k] = ""
            writer.writerow(row)

    return output_path


def export_clean_packet(
    run_data: dict,
    output_path: Path,
    export_format: str = "json",
    sort: SortSpec | None = None,
    top: int | None = None,
    rank: bool = False,
    claim_filter: ClaimFilter | None = None,
    bias_map: dict[str, float] | None = None,
    score_field: str | None = None,
) -> dict:
    """Validate and serialize verified records into a closed packet or CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    verified_records, task = verified_records_from_snapshot(run_data)
    receipts: list[ProviderReceipt] = []

    item_route_map = _build_item_route_map(run_data) if bias_map and score_field else None
    verified_records = _filter_and_sort_records(
        verified_records,
        sort=sort,
        top=top,
        claim_filter=claim_filter,
        bias_map=bias_map,
        score_field=score_field,
        item_route_map=item_route_map,
    )

    raw_receipts = run_data.get("model_runs")
    if isinstance(raw_receipts, list):
        receipts = [ProviderReceipt.model_validate(receipt) for receipt in raw_receipts]
    else:
        receipts = [
            ProviderReceipt.model_validate(batch["receipt"])
            for batch in run_data.get("batches", {}).values()
            if batch.get("receipt")
        ]
    total_tokens = sum(
        _as_int(receipt.usage.get("total_tokens"))
        for receipt in receipts
        if isinstance(receipt.usage, dict)
    )
    total_cost = sum(receipt.cost for receipt in receipts if receipt.cost is not None)

    policy = RoutePolicy.model_validate(run_data["policy"]) if run_data.get("policy") else None

    packet_model = CleanPacket.model_validate({
        "format_version": "harness_fleet_v2",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_data.get("run_id"),
        "task": task,
        "task_revision": run_data["task_revision"],
        "input_digest": run_data["input_digest"],
        "total_verified_records": len(verified_records),
        "audit": {
            "total_batches_processed": len(run_data.get("batches", {})),
            "total_tokens_consumed": total_tokens,
            "total_cost_reported": total_cost,
            "batches_verified": sum(1 for b in run_data.get("batches", {}).values() if b.get("status") == "verified"),
            "batches_failed": sum(1 for b in run_data.get("batches", {}).values() if b.get("status") == "failed"),
            "model_attempts": int(run_data.get("attempts_used", len(receipts))),
            "receipts_recorded": len(receipts),
            "unknown_cost_attempts": sum(1 for receipt in receipts if receipt.cost is None),
        },
        "records": verified_records,
        "receipts": receipts,
        "policy": policy,
    })
    packet = packet_model.model_dump(mode="json", by_alias=True)

    if export_format == "csv" or output_path.suffix.lower() == ".csv":
        export_clean_csv(
            run_data,
            output_path,
            sort=sort,
            top=top,
            rank=rank,
            claim_filter=claim_filter,
            bias_map=bias_map,
            score_field=score_field,
        )
    elif export_format == "jsonl" or output_path.suffix.lower() == ".jsonl":
        # One JSON record per line, with flattened claims + quote
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for idx, rec in enumerate(verified_records, start=1):
                flat = {
                    "item_id": rec.item_id,
                    "source_uri": rec.source_uri,
                    "source_digest": rec.source_digest,
                    **rec.claims,
                    "primary_quote_text": rec.quotes[0].text if rec.quotes else "",
                    "quote_count": len(rec.quotes),
                    "quotes": [q.model_dump(mode="json") for q in rec.quotes],
                }
                if rank:
                    flat["rank"] = idx
                f.write(json.dumps(flat, ensure_ascii=False) + "\n")
    else:
        output_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")

    return packet

