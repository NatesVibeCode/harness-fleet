#!/usr/bin/env python3
"""Bake the offline preview boards from the board's own payload builder.

The three lane previews under ``docs/boards/`` are the pages the design gallery
and the UI crawl open. They carry an embedded payload so they need no server.

That payload used to be written by hand, which quietly made it a second schema:
the pages said ``candidate`` where the renderer reads ``name``,
``quote``/``source_url`` where it reads ``text``/``source.uri``, and carried no
``facets`` at all — so the Scorecard view of every one of them threw, and the
Evidence view printed the word "undefined" where a quote should be.

There is one writer now. Each page is built from a payload that
``build_board_payload`` actually produced, so its shape cannot drift from the
board; the aggregates are recomputed with the board's own helpers for the same
reason. The words come from the curated rows below, because the deterministic
demo provider fabricates schema-valid but contentless claims ("demo verified
value") and a design gallery still needs something to look at.

    python3 tools/build_preview_boards.py           # write the pages
    python3 tools/build_preview_boards.py --check   # fail when they are stale
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The board's own helpers, imported rather than reimplemented: a preview that
# computed its facets its own way would be the divergence this script exists to
# end, one layer down.
from harness_fleet.board import (  # noqa: E402
    BOARD_HTML_PATH,
    _counter,
    _field_specs,
    _highlights,
    build_board_payload,
)
from harness_fleet.catalog import PriceState, RouteCatalog  # noqa: E402
from harness_fleet.engine import Engine  # noqa: E402
from harness_fleet.ledger import Ledger  # noqa: E402
from harness_fleet.models import RoutePolicy, score_to_fit_tier  # noqa: E402
from harness_fleet.store import HarnessStore  # noqa: E402
from harness_fleet.task import create_task_from_preset  # noqa: E402

OUT_DIR = ROOT / "docs" / "boards"
DEMO_ROUTE = "demo/preview"
# Pinned so a rebake is reviewable as a diff instead of a wall of new timestamps.
STAMP = "2026-09-17T16:00:00+00:00"
# The running list shows a "moved" delta, so every entity gets two pinned visits.
# Two constants rather than two clock reads is what keeps a rebake byte-identical.
EARLIER = "2026-08-20T16:00:00+00:00"


# --------------------------------------------------------------------------
# What each preview shows. `checklist` is a subset of the lane's real checklist;
# anything unlisted is false, and the score never exceeds the points earned.
# --------------------------------------------------------------------------
LANES: dict[str, dict[str, Any]] = {
    "account": {
        "run_id": "account-demo-01",
        "task": "account-research",
        "preset": ("account_research", "account-research"),
        "detail_claim": "identified_gap",
        "hypothesis_claim": None,
        "nested_answers": False,
        "rows": [
            {
                "id": "fintech-apex.com",
                "name": "Apex Financial Cloud",
                "site": "https://fintech-apex.com/blog/payments-on-kafka",
                "source_text": "Apex Financial is migrating its core payments ledger to Kafka and Snowflake, the Q3 engineering post says.",
                "score": 92,
                "checklist": {
                    "explicit_initiative": True,
                    "stack_confirmed": True,
                    "hiring_or_trigger": True,
                    "firmographic_fit": True,
                },
                "detail": "payments ledger moving to Kafka and Snowflake, named in the Q3 engineering post",
                "reasoning": "the initiative is named in their own post, the stack is confirmed twice, and two platform roles opened the same week",
                "quotes": [
                    "Apex Financial is migrating its core payments ledger to Kafka and Snowflake"
                ],
            },
            {
                "id": "logix-scale.com",
                "name": "Logix Global Logistics",
                "site": "https://logix-scale.com/engineering/freight-events",
                "source_text": "Logix rebuilt freight tracking on Kafka last quarter and is hiring two streaming engineers in Austin.",
                "score": 78,
                "checklist": {
                    "explicit_initiative": True,
                    "stack_confirmed": True,
                    "firmographic_fit": True,
                },
                "detail": "freight tracking rebuilt on Kafka; no warehouse layer named yet",
                "reasoning": "an explicit rebuild is quoted and the stack is confirmed, but nothing in the source points at a buying trigger beyond the two open roles",
                "quotes": ["Logix rebuilt freight tracking on Kafka last quarter"],
            },
            {
                "id": "retailnext-corp.com",
                "name": "RetailNext Commerce",
                "site": "https://retailnext-corp.com/notes/peak-load",
                "source_text": "RetailNext describes a peak-season queue backlog and says the platform team is reviewing its event pipeline.",
                "score": 61,
                "checklist": {
                    "explicit_initiative": True,
                    "hiring_or_trigger": True,
                    "firmographic_fit": True,
                },
                "detail": "peak-season queue backlog under review; the stack is described only as an event pipeline",
                "reasoning": "the bottleneck is quoted and a trigger exists, but the source never names the technology, so the stack stays unconfirmed",
                "quotes": ["RetailNext describes a peak-season queue backlog"],
            },
        ],
    },
    "partner": {
        "run_id": "partner-demo-01",
        "task": "partner-research",
        "preset": ("partner_research", "partner-research"),
        "detail_claim": "identified_practice",
        "hypothesis_claim": "revenue_hypothesis",
        "nested_answers": True,
        "rows": [
            {
                "id": "cloud_solutions.io",
                "name": "Cloud Solutions Group",
                "site": "https://cloud_solutions.io/customers/fintech-migration",
                "source_text": "Cloud Solutions Group is a certified Snowflake implementation partner that delivered a Kafka migration for a top-ten fintech.",
                "score": 90,
                "checklist": {
                    "q1_billable_delivery": True,
                    "q2_stack_delivery": True,
                    "q3_delivery_hiring": True,
                    "q4_client_outcome": True,
                    "q5_commercial_scale": True,
                    "q6_vendor_alliance": True,
                    "q7_vertical_focus": True,
                    "q8_independent_validation": True,
                    "q9_published_engineering": True,
                    "q10_growth_signal": True,
                },
                "detail": "Snowflake implementation partner with a delivered Kafka migration",
                "hypothesis": "co-sell into fintech accounts where the migration motion is already funded",
                "reasoning": "billable delivery, the named stack, a published client outcome and an elite vendor alliance all appear in first-party sources",
                "answers": {
                    "target_stack": ["Snowflake", "Kafka", "dbt", "Airflow"],
                    "service_model": "migration_modernization",
                    "industry_verticals": ["fintech", "payments"],
                    "delivery_coverage": "US and Canada, 120 delivery engineers",
                    "vendor_alliances": ["Snowflake Elite", "AWS Premier"],
                    "case_study_outcome": "cut settlement reporting from nine hours to forty minutes",
                    "client_logos": ["Northwind Bank", "Vector Pay"],
                    "hiring_signals": ["3 data engineers", "1 solutions architect"],
                    "revenue_motion": "co_sell",
                    "third_party_mentions": ["Snowflake partner directory"],
                    "engineering_output": ["dbt packages", "Kafka connector fork"],
                    "growth_signals": ["opened a Toronto office"],
                    "evidence_categories": ["customer story", "partner directory"],
                },
                "quotes": [
                    "Cloud Solutions Group is a certified Snowflake implementation partner",
                    "delivered a Kafka migration for a top-ten fintech",
                ],
            },
            {
                "id": "northstar-data.example",
                "name": "Northstar Data Partners",
                "site": "https://northstar-data.example/case-studies/retail",
                "source_text": "Northstar Data Partners runs ClickHouse and dbt modernisation projects for retail clients and publishes its reference architectures.",
                "score": 74,
                "checklist": {
                    "q1_billable_delivery": True,
                    "q2_stack_delivery": True,
                    "q3_delivery_hiring": True,
                    "q4_client_outcome": True,
                    "q6_vendor_alliance": True,
                    "q8_independent_validation": True,
                },
                "detail": "ClickHouse and dbt modernisation for retail",
                "hypothesis": "referral motion into retail accounts already running ClickHouse",
                "reasoning": "the delivery model and stack are clear and a client outcome is published, but the alliance is only a reseller listing",
                "answers": {
                    "target_stack": ["ClickHouse", "dbt", "Terraform"],
                    "service_model": "migration_modernization",
                    "industry_verticals": ["retail", "ecommerce"],
                    "delivery_coverage": "UK and Ireland",
                    "vendor_alliances": ["ClickHouse Reseller"],
                    "case_study_outcome": "halved batch window for a grocery chain",
                    "client_logos": ["Marchwood Retail"],
                    "hiring_signals": ["2 analytics engineers"],
                    "revenue_motion": "referral",
                    "engineering_output": ["reference architectures"],
                    "evidence_categories": ["case study", "engineering blog"],
                },
                "quotes": [
                    "Northstar Data Partners runs ClickHouse and dbt modernisation projects for retail clients"
                ],
            },
            {
                "id": "harbor-systems.example",
                "name": "Harbor Systems",
                "site": "https://harbor-systems.example/services",
                "source_text": "Harbor Systems provides managed services around PostgreSQL and offers staff augmentation for platform teams.",
                "score": 54,
                "checklist": {
                    "q1_billable_delivery": True,
                    "q2_stack_delivery": True,
                    "q4_client_outcome": True,
                    "q6_vendor_alliance": True,
                },
                "detail": "managed PostgreSQL services with staff augmentation",
                "hypothesis": "subcontract capacity for overflow migration work",
                "reasoning": "delivery is project-based and a client outcome is named, but there is no published engineering output and no independent validation",
                "answers": {
                    "target_stack": ["PostgreSQL", "Terraform"],
                    "service_model": "managed_services",
                    "industry_verticals": ["healthcare"],
                    "delivery_coverage": "US remote",
                    "vendor_alliances": ["AWS Advanced"],
                    "case_study_outcome": "migrated 40 databases with no downtime",
                    "client_logos": ["Beacon Health"],
                    "hiring_signals": ["1 site reliability engineer"],
                    "revenue_motion": "subcontract",
                    "evidence_categories": ["case study"],
                },
                "quotes": [
                    "Harbor Systems provides managed services around PostgreSQL"
                ],
            },
            {
                "id": "pixel-forge.example",
                "name": "Pixel Forge Studio",
                "site": "https://pixel-forge.example/about",
                "source_text": "Pixel Forge Studio is a five-person design studio that also resells SaaS licences.",
                "score": 44,
                "checklist": {
                    "q1_billable_delivery": True,
                    "q2_stack_delivery": True,
                    "q4_client_outcome": True,
                },
                "detail": "five-person design studio reselling licences",
                "hypothesis": "not an implementation partner for this motion",
                "reasoning": "the source shows licence resale and design work, no delivery engineering, no alliances and no published outcomes",
                "answers": {
                    "target_stack": ["Figma"],
                    "service_model": "staff_aug_only",
                    "industry_verticals": ["marketing"],
                    "delivery_coverage": "one timezone",
                    "revenue_motion": "reseller",
                    "client_logos": ["Cobalt Labs"],
                    "evidence_categories": ["about page"],
                },
                "quotes": [
                    "Pixel Forge Studio is a five-person design studio that also resells SaaS licences"
                ],
            },
        ],
    },
    "career": {
        "run_id": "career-demo-01",
        "task": "triage",
        "preset": ("career_research", "career-research"),
        "detail_claim": "role_focus",
        "hypothesis_claim": "fit_hypothesis",
        "nested_answers": True,
        "rows": [
            {
                "id": "stripe-enterprise-ae",
                "site": "https://stripe.example/jobs/enterprise-account-executive",
                "source_text": "Stripe is hiring an Enterprise Account Executive for financial services, remote in the US, carrying a 1.2M quota.",
                "score": 88,
                "checklist": {
                    "q1_target_function": True,
                    "q2_quota_ownership": True,
                    "q3_seniority": True,
                    "q4_remote_or_territory": True,
                    "q5_comp_published": True,
                    "q6_relevant_stack": True,
                    "q7_enterprise_buyers": True,
                    "q8_team_scope": True,
                    "q9_live_requisition": True,
                    "q10_recency": True,
                },
                "detail": "enterprise new-logo selling into financial services",
                "hypothesis": "the quota, the buyer and the territory all line up with the target profile",
                "reasoning": "the posting names the function, an owned quota, a published range and a live requisition with a date",
                "answers": {
                    "employer": "Stripe",
                    "function": "enterprise sales",
                    "seniority": "senior director",
                    "territory": "US remote",
                    "compensation": "$240k–$280k OTE",
                    "stack": ["Salesforce", "Looker"],
                    "buyers": ["VP Engineering", "CTO"],
                    "requisition_url": "https://stripe.example/jobs/enterprise-account-executive",
                    "posted_at": "2026-09-14",
                },
                "quotes": [
                    "Stripe is hiring an Enterprise Account Executive for financial services, remote in the US, carrying a 1.2M quota"
                ],
            },
            {
                "id": "notion-sales-ops",
                "site": "https://notion.example/careers/sales-operations-lead",
                "source_text": "Notion is looking for a Sales Operations Lead to own forecasting and territory design for the commercial segment.",
                "score": 72,
                "checklist": {
                    "q1_target_function": True,
                    "q3_seniority": True,
                    "q4_remote_or_territory": True,
                    "q5_comp_published": True,
                    "q7_enterprise_buyers": True,
                    "q9_live_requisition": True,
                    "q10_recency": True,
                },
                "detail": "sales operations ownership for the commercial segment",
                "hypothesis": "ops scope is right, but the segment sits below the enterprise buyers the profile wants",
                "reasoning": "the function, seniority and location are stated and pay is published; the quota is a team target rather than an individual one",
                "answers": {
                    "employer": "Notion",
                    "function": "sales operations",
                    "seniority": "lead",
                    "territory": "US remote",
                    "compensation": "$180k–$210k",
                    "stack": ["Salesforce", "dbt"],
                    "buyers": ["Head of Sales"],
                    "requisition_url": "https://notion.example/careers/sales-operations-lead",
                    "posted_at": "2026-09-11",
                },
                "quotes": [
                    "Notion is looking for a Sales Operations Lead to own forecasting and territory design"
                ],
            },
            {
                "id": "acme-sdr",
                "site": "https://acme.example/jobs/sales-development-representative",
                "source_text": "Acme is hiring a Sales Development Representative to book meetings for the mid-market team.",
                "score": 54,
                "checklist": {
                    "q1_target_function": True,
                    "q3_seniority": True,
                    "q4_remote_or_territory": True,
                    "q9_live_requisition": True,
                    "q10_recency": True,
                },
                "detail": "inbound meeting booking for mid-market",
                "hypothesis": "adjacent to the profile: the function is right, the altitude is not",
                "reasoning": "a live, dated requisition in the target function, but no quota ownership, no published range and no enterprise buyers",
                "answers": {
                    "employer": "Acme",
                    "function": "sales development",
                    "seniority": "representative",
                    "territory": "US remote",
                    "compensation": "",
                    "stack": ["Outreach"],
                    "buyers": [],
                    "requisition_url": "https://acme.example/jobs/sales-development-representative",
                    "posted_at": "2026-09-09",
                },
                "quotes": [
                    "Acme is hiring a Sales Development Representative to book meetings for the mid-market team"
                ],
            },
        ],
    },
}


def _reference_run(lane: str, tmp: Path) -> tuple[HarnessStore, Any]:
    """A real, verified run through the lane's own preset and the demo provider."""
    spec = LANES[lane]
    store = HarnessStore(tmp / f"{lane}.db")
    catalog = RouteCatalog(db_path=store.path)
    catalog.add_route(
        route_id=DEMO_ROUTE,
        provider="demo",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        enabled=True,
        price_state=PriceState.PRICE_OBSERVED_ZERO.value,
        verification_source="preview bake (deterministic)",
    )
    task = create_task_from_preset(*spec["preset"])
    store.register_task(task)
    Engine(
        task=task,
        store=store,
        policy=RoutePolicy(allowed_routes=[DEMO_ROUTE], free_only=True),
    ).run_campaign(
        raw_items=[
            {"item_id": row["id"], "text": row["source_text"]} for row in spec["rows"]
        ],
        run_id=spec["run_id"],
        input_path=f"<preview>/{lane}.csv",
        output_packet_path=tmp / f"{lane}-packet.json",
        concurrency=1,
    )
    # The campaign writes a score history for the demo items whose claims this
    # preview replaces. Left in place it would re-stamp the fixture with the wall
    # clock on every bake, so the fixture keeps the schema and drops the scaffolding.
    with store.connect() as connection:
        connection.execute("DELETE FROM score_history")
    return store, task


def _curated_row(
    template: dict[str, Any], spec: dict[str, Any], task: Any, lane: str, page: dict[str, Any]
) -> dict[str, Any]:
    """One preview row, wearing the exact shape the builder produced."""
    row = copy.deepcopy(template)
    points = dict(task.checklist)
    checklist = {item: bool(spec["checklist"].get(item)) for item in task.checklist}
    earned = sum(points[item] for item, passed in checklist.items() if passed)
    score = int(spec["score"])
    tier = score_to_fit_tier(score)
    detail = spec["detail"]

    claims: dict[str, Any] = {
        "checklist": checklist,
        "score": score,
        "fit_tier": tier,
        "reasoning": spec["reasoning"],
    }
    claims[page["detail_claim"]] = detail
    if page["hypothesis_claim"]:
        claims[page["hypothesis_claim"]] = spec.get("hypothesis") or detail
    if page["nested_answers"]:
        answers = dict(spec["answers"])
        claims["answers"] = answers
    else:
        # The account schema has no nested `answers`; the board promotes the
        # top-level claim instead, which is why that lane shows one attribute.
        answers = {page["detail_claim"]: detail}

    true_items = sorted(item for item, passed in checklist.items() if passed)
    quotes = [
        {
            "text": text,
            "supports": true_items,
            "slice_id": f"preview-{spec['id']}-{index}",
            "start": 0,
            "end": len(text),
        }
        for index, text in enumerate(spec["quotes"])
    ]

    source = copy.deepcopy(row["source"])
    source.update(
        {
            "uri": spec["site"],
            "digest": hashlib.sha256(spec["source_text"].encode("utf-8")).hexdigest(),
            "content_type": "text/html",
            "captured_at": STAMP,
            "scored_at": STAMP,
        }
    )
    provenance = copy.deepcopy(row["provenance"])
    provenance.update(
        {
            "route": DEMO_ROUTE,
            "provider": "demo",
            "cost": 0.0,
            "cost_status": "reported_zero",
            "attempts": 1,
            "status": "complete",
            "duration_seconds": 0.004,
        }
    )

    row.update(
        {
            "id": spec["id"],
            "name": spec.get("name") or answers.get("employer") or spec["id"],
            "score": score,
            "tier": tier,
            "tier_supported": "",
            "highlights": _highlights({**claims, **answers}),
            "tier_capped": False,
            "checklist": checklist,
            "checklist_points": points,
            "earned_points": earned,
            "practice": detail,
            "detail": detail,
            "hypothesis": claims.get(page["hypothesis_claim"]) if page["hypothesis_claim"] else detail,
            "reasoning": spec["reasoning"],
            "action_url": spec["site"],
            "action_label": "Apply ↗" if lane == "career" else "Site ↗",
            "answers": answers,
            "claims": claims,
            "quotes": quotes,
            "source": source,
            "provenance": provenance,
            "diversity": len({quote["text"] for quote in quotes}),
            "evidence": {},
        }
    )
    return row


def _rebuild_aggregates(
    payload: dict[str, Any], task: Any, rows: list[dict[str, Any]], lane: str, run_id: str
) -> None:
    """Recompute everything the builder derives from the rows it was given."""
    payload["run"] = {
        **payload["run"],
        "run_id": run_id,
        "status": "completed",
        "created_at": STAMP,
        "finished_at": STAMP,
        "total_items": len(rows),
        "attempts_used": len(rows),
        "input_path": f"<preview>/{lane}.csv",
        "task": task.name,
        "lane": lane,
        "task_revision": "preview",
        "instructions": "",
    }

    payload["checklist"] = [
        {
            **entry,
            "points": task.checklist.get(entry["item_id"], entry["points"]),
            "passed": sum(1 for row in rows if row["checklist"].get(entry["item_id"])),
        }
        for entry in payload["checklist"]
    ]
    payload["checklist_total"] = sum(task.checklist.values())

    # The same promotion rule the builder uses, so a lane without a nested
    # `answers` object still gets its top-level claim as a column.
    first = rows[0]
    answers_spec = _field_specs(task, ("answers",), first["answers"])
    generic_spec = _field_specs(task, (), first["claims"])
    candidates = answers_spec or [
        spec
        for spec in generic_spec
        if spec["key"] not in ("score", "checklist", "fit_tier", "reasoning")
    ]
    payload["attributes"] = [
        spec
        for spec in candidates
        if any(
            (row.get("answers") or {}).get(spec["key"]) not in (None, "", [], {})
            for row in rows
        )
    ]
    facets: dict[str, dict[str, int]] = {}
    for spec in candidates:
        key = spec["key"]
        if spec["type"] == "list":
            facets[key] = _counter(
                [value for row in rows for value in (row["answers"].get(key) or [])]
            )
        else:
            facets[key] = _counter([row["answers"].get(key) for row in rows])
    payload["facets"] = facets

    scores = [row["score"] for row in rows if isinstance(row["score"], (int, float))]
    payload["stats"] = {
        **payload["stats"],
        "records": len(rows),
        "scored": len(scores),
        "unscored": len(rows) - len(scores),
        "average_score": round(sum(scores) / len(scores), 1) if scores else None,
        "best_score": max(scores) if scores else None,
        "tier_counts": _counter([row["tier"] for row in rows]),
        "quote_count": sum(len(row["quotes"]) for row in rows),
        "records_with_quotes": sum(1 for row in rows if row["quotes"]),
        "routes": [DEMO_ROUTE],
        "cost_reported": 0.0,
        "batches_verified": len(rows),
        "batches_failed": 0,
        "batches_total": len(rows),
        "attempts_used": len(rows),
        "tier_capped": 0,
        "evidence_kinds": {},
        "lone_claims": 0,
    }
    payload["records_rejected"] = 0


def _embed(html: str, payload: dict[str, Any]) -> str:
    """Put the payload where the board looks for it, and nowhere else."""
    anchor = "</style>\n</head>"
    if html.count(anchor) != 1:
        raise SystemExit("the board template lost its single </style></head> anchor")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    script = f"<script>window.__INITIAL_BOARD_DATA__ = {blob};</script>"
    return html.replace(anchor, f"</style>\n{script}\n</head>", 1)


@contextlib.contextmanager
def _frozen_clock():
    """Pin the store's clock for the duration of a bake.

    A preview that re-stamped itself with the wall clock would show up as a diff
    on every rebuild, which makes the checked-in pages unreviewable.
    """
    from harness_fleet import store as store_module

    original = store_module.now_iso
    store_module.now_iso = lambda: STAMP
    try:
        yield
    finally:
        store_module.now_iso = original


def build_payload(lane: str, tmp: Path) -> dict[str, Any]:
    """The payload one preview page embeds."""
    spec = LANES[lane]
    with _frozen_clock():
        store, task = _reference_run(lane, tmp)
        # The running list is real, not a second assembly: the curated rows go
        # through the same call the graph uses, so the lane resolves and the ledger
        # view is the ledger view. Two visits each, so "moved" has something to say.
        ledger = Ledger(store)

        def visits(at: str, drop: float) -> list[dict[str, Any]]:
            return [
                {
                    "item_id": row["id"],
                    "candidate": row["id"],
                    "outcome": "read",
                    "gates": [],
                    "score": float(row["score"]) - drop,
                    "tier": score_to_fit_tier(row["score"] - drop),
                    "run_id": spec["run_id"],
                }
                for row in spec["rows"]
            ]

        ledger.record(
            visits(EARLIER, 8), dag_id=f"{lane}-preview", node_id="s-surface", lane=lane, at=EARLIER
        )
        ledger.record(
            visits(STAMP, 0), dag_id=f"{lane}-preview", node_id="s-score", lane=lane, at=STAMP
        )
        payload = build_board_payload(store.path, spec["run_id"])

    if not payload["partners"]:
        raise SystemExit(
            f"{lane}: the demo run produced no verified record to copy a shape from"
        )
    template = payload["partners"][0]
    rows = [_curated_row(template, row, task, lane, spec) for row in spec["rows"]]
    payload["partners"] = rows
    _rebuild_aggregates(payload, task, rows, lane, spec["run_id"])
    return payload


def reference_payload(lane: str, tmp: Path) -> dict[str, Any]:
    """A payload straight from ``build_board_payload``, for shape comparison.

    A preview that embeds a payload of its own invention is the defect this whole
    module exists to prevent, so the test compares against the real thing rather
    than against the baker.
    """
    with _frozen_clock():
        store, _ = _reference_run(lane, tmp)
        return build_board_payload(store.path, LANES[lane]["run_id"])


def build_pages() -> dict[str, str]:
    """``{filename: html}`` for every preview page, template and payload together."""
    template = BOARD_HTML_PATH.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        return {
            f"{lane}-lane.html": _embed(template, build_payload(lane, tmp))
            for lane in LANES
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero when a committed page differs from a fresh bake.",
    )
    args = parser.parse_args()

    pages = build_pages()
    stale: list[str] = []
    for name, html in pages.items():
        target = OUT_DIR / name
        if args.check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != html:
                stale.append(name)
            continue
        target.write_text(html, encoding="utf-8")
        print(f"wrote {target.relative_to(ROOT)} ({len(html.splitlines())} lines)")

    if args.check and stale:
        print(
            "stale preview board(s): " + ", ".join(stale) + "\n"
            "run: python3 tools/build_preview_boards.py",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print(f"{len(pages)} preview board(s) match a fresh bake")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
