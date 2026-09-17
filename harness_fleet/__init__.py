"""Harness Fleet: Coordinated CLI-harness worker fleet with closed fields and exact source evidence."""
__version__ = "5.0"
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .catalog import RouteCatalog
from .discover import fetch_text, run_discovery
from .engine import Engine
from .export import export_clean_packet
from .grounding import verify_grounding
from .input_data import iter_input_items, load_input_items
from .models import (
    CleanPacket,
    ExtractedItem,
    InputItem,
    ModelOutput,
    QuoteRef,
    TaskSpec,
)
from .packer import pack_items
from .profile import IdealCompanyProfile
from .slicer import slice_document
from .store import HarnessStore
from .task import load_task_spec


def read_packet(packet_path: str) -> dict:
    """Convenience helper for downstream trusted applications to safely load a clean packet."""
    p = Path(packet_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Packet file not found: {packet_path}")
    return CleanPacket.model_validate_json(p.read_text(encoding="utf-8")).model_dump(mode="json", by_alias=True)

def process(
    task: str | TaskSpec | Path,
    input: str | Path | Iterable[InputItem | dict[str, Any]],
    run_id: str | None = None,
    concurrency: int = 4,
    max_attempts: int = 300,
    output: str | Path | None = None,
    policy=None,
    profile: IdealCompanyProfile | str | Path | None = None,
    db: str | Path | None = None,
    use_active_profile: bool = False,
    **load_kwargs,
) -> dict:
    """High-level Python SDK: `harness_fleet.process(task, input, ...) -> packet`.

    Example:
        import harness_fleet
        packet = harness_fleet.process("my-task", "data.csv", concurrency=8)
        df = packet["records"]  # or use export helpers

    Supports local paths (.csv/.jsonl/.json/.txt/.html/.pdf), lists of InputItem,
    or pandas DataFrames (if installed) via `input=df`.
    Profiles are opt-in: pass `profile=...` or set `use_active_profile=True`.
    """
    import time as _time

    # Resolve task
    from pathlib import Path as _P  # noqa: N814

    store = HarnessStore(_P(db)) if db is not None else HarnessStore()
    if isinstance(task, TaskSpec):
        spec = task
        store.register_task(spec)
    elif isinstance(task, (str, _P)) and _P(str(task)).is_file():
        spec = load_task_spec(str(task))
        store.register_task(spec)
    else:
        spec = store.get_task(str(task))

    # Resolve input items (DataFrame support optional)
    items: Any
    raw_items_factory = None
    input_path_str = str(input) if isinstance(input, (str, _P)) else "python-sdk"
    if isinstance(input, list):
        # Keep caller-provided lists lazy through the packer; it validates each
        # record at the same trust boundary as file-backed input.
        items = input
    elif isinstance(input, (str, _P)) and _P(str(input)).is_file():
        source = _P(str(input))
        input_options = dict(load_kwargs)
        def raw_items_factory():
            return iter_input_items(source, **input_options)
        items = raw_items_factory()
    else:
        # Try DataFrame-like (has to_dict)
        try:
            if hasattr(input, "to_dict") and hasattr(input, "columns"):
                # pandas DataFrame: itertuples avoids creating a second full
                # list of row dictionaries. The fallback keeps support for
                # DataFrame-like objects that only expose to_dict().
                def dataframe_items():
                    columns = list(input.columns)
                    if hasattr(input, "itertuples"):
                        rows = (
                            dict(zip(columns, values, strict=False))
                            for values in input.itertuples(index=False, name=None)
                        )
                    else:
                        rows = iter(input.to_dict(orient="records"))
                    for idx, rec in enumerate(rows):
                        # Prefer explicit id column
                        cand_id = rec.get("item_id") or rec.get("id") or f"row_{idx}"
                        txt = rec.get("text") or rec.get("body") or rec.get("content") or rec.get("description") or ""
                        if not txt:
                            # fallback: first stringifiable column value
                            for v in rec.values():
                                if isinstance(v, str) and len(v.strip()) > 20:
                                    txt = v
                                    break
                        yield InputItem(
                            item_id=str(cand_id),
                            text=str(txt),
                            metadata={k: v for k, v in rec.items() if k not in ("item_id", "id", "text", "body", "content")},
                        )

                raw_items_factory = dataframe_items
                items = raw_items_factory()
            elif isinstance(input, Mapping):
                raise TypeError("input mappings are not records; pass an iterable of InputItem/dict records")
            elif isinstance(input, Iterable):
                # Generators and other one-shot iterables are consumed once by
                # Engine, which keeps ingestion bounded without requiring a
                # second pass or a full in-memory copy.
                items = input
            else:
                raise TypeError("unsupported input type")
        except Exception as e:
            raise ValueError(f"Unable to resolve input '{input}': {e}") from e

    rid = run_id or f"sdk-{_time.time_ns()}-{uuid.uuid4().hex[:8]}"
    out_path = _P(output) if output is not None else _P(f"runs/{rid}/clean_packet.json")
    selected_profile = None
    profile_revision_id = None
    if profile is not None:
        selected_profile = (
            profile
            if isinstance(profile, IdealCompanyProfile)
            else IdealCompanyProfile.load(profile)
        )
        profile_revision_id = store.save_profile(selected_profile)
    elif use_active_profile:
        profile_revision_id = store.active_profile_revision_id("ideal_company")
    if selected_profile is None and profile_revision_id:
        selected_profile = store.load_profile("ideal_company")
    eng = Engine(task=spec, store=store, policy=policy, profile=selected_profile)
    return eng.run_campaign(
        raw_items=items,
        run_id=rid,
        input_path=input_path_str,
        concurrency=concurrency,
        max_attempts=max_attempts,
        output_packet_path=out_path,
        profile_revision_id=profile_revision_id,
        profile=selected_profile,
        raw_items_factory=raw_items_factory,
    )

__all__ = [
    "load_task_spec",
    "IdealCompanyProfile",
    "Engine",
    "RouteCatalog",
    "verify_grounding",
    "CleanPacket",
    "ExtractedItem",
    "InputItem",
    "ModelOutput",
    "QuoteRef",
    "TaskSpec",
    "HarnessStore",
    "load_input_items",
    "iter_input_items",
    "fetch_text",
    "run_discovery",
    "slice_document",
    "pack_items",
    "export_clean_packet",
    "read_packet",
    "process",
    "__version__",
]
