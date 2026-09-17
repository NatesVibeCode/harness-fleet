"""What the onboarding supplies to a lane's questions.

A lane declares the *shape* of the questions its product asks — its templates,
its surfaces, its bar. The onboarding profile declares the *values*: the stack,
the industries, the roles, the pain. Before this module the two never met. A
lane could only ever search its own hard-coded literals, so ``partner.json``
carried a ``query_terms`` list of technologies that duplicated exactly what the
Ideal Partner Profile is authored to say.

There is no separate "seed" concept, and no company ever becomes a query. An
onboarding profile states **traits**, and traits are what generalize over a
population: "Kafka" and "fintech" return firms nobody has met, while a company
name returns the one firm the operator already knew. An anchor logo is a
calibration exemplar — the IEP contract says so in as many words, "not a direct
deterministic score" — so it belongs in the scoring prompt as context, and in an
exclusion list where one exists, never in the search.

The join is one-way and additive: an axis the profile states *overrides* the
lane's illustrative values for that axis, and an axis it does not state keeps
the lane's. A fresh install with no onboarding therefore behaves exactly as it
did, and an operator who has done the onboarding gets the onboarding searched.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

#: The axes a lane template may name. A profile translates its own vocabulary
#: into these, so a lane reads the same axis whatever the profile kind.
#:
#: There is deliberately **no company axis**. A firm's name as a query turns
#: discovery into a lookup: it returns that one company's own pages, which can
#: never be evidence *about* anybody else. A trait generalizes over a
#: population; a company identifies a single instance.
AXES = ("tech", "vertical", "role", "pain")

#: A template placeholder: ``{tech}``, ``{vertical}``, ``{role}``, ``{pain}``.
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def profile_query_terms(profile: Any) -> dict[str, list[str]]:
    """The axis values an onboarding profile supplies.

    Asks the profile to translate, the same way the gate engine asks for
    ``funnel_profile``: a profile type states its own vocabulary and hands back
    the axes, rather than this module guessing at field names that three
    contracts happen to share. A profile that cannot translate supplies nothing,
    which is the honest reading rather than an invention.
    """
    if profile is None:
        return {}
    adapt = getattr(profile, "query_terms", None)
    if not callable(adapt):
        return {}
    try:
        adapted = adapt()
    except Exception:
        return {}
    if not isinstance(adapted, dict):
        return {}
    out: dict[str, list[str]] = {}
    for axis, values in adapted.items():
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set, frozenset)):
            continue
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if cleaned:
            out[str(axis)] = cleaned
    return out


def lane_query_terms(lane: Any, profile: Any) -> dict[str, list[str]]:
    """The lane's query axes, with the profile's values where it states them.

    Profile first on every axis it speaks to: the whole point of onboarding is
    that the operator said what this engagement is about, and a lane's
    illustrative literals must not outvote it. An axis the profile leaves alone
    keeps whatever the lane shipped, so the lane is still runnable alone.
    """
    merged: dict[str, list[str]] = {}
    for name, values in (getattr(lane, "query_terms", None) or {}).items():
        if isinstance(values, (list, tuple, set, frozenset)):
            merged[str(name)] = [str(value) for value in values]
    for axis, values in profile_query_terms(profile).items():
        merged[axis] = list(values)
    return merged


def expand(templates: Any, terms: dict[str, list[str]]) -> list[str]:
    """Templates filled one query per combination of the axes they name.

    Shared with the lane-only expander so an onboarded run and an un-onboarded
    one cannot drift into two different ideas of what a template means.

    A template naming an axis nothing supplies is *skipped*, not emitted with a
    literal brace in it: `{role}` is not a search, and a lane that templates an
    axis its profile has not filled should ask fewer questions rather than one
    that cannot be answered.
    """
    queries = [str(query) for query in (templates or []) if str(query).strip()]
    expanded: list[str] = []
    for query in queries:
        placeholders = PLACEHOLDER.findall(query)
        unfilled = [
            name for name in placeholders if name not in terms or not terms[name]
        ]
        if unfilled:
            continue
        named = [name for name in placeholders if name in terms and terms[name]]
        if not named:
            expanded.append(query)
            continue
        for combination in itertools.product(*(terms[name] for name in named)):
            filled = query
            for name, value in zip(named, combination, strict=True):
                filled = filled.replace("{" + name + "}", value)
            expanded.append(filled)
    seen: list[str] = []
    for query in expanded:
        if query not in seen:
            seen.append(query)
    return seen


def lane_queries(lane: Any, profile: Any, *, cap: int = 0) -> list[str]:
    """The lane's templates filled with the onboarding profile's values.

    ``cap`` bounds one run's breadth; uncapped is the space a quota widens
    through. Zero means the lane's own ``max_queries``, then no bound.
    """
    queries = expand(getattr(lane, "queries", None), lane_query_terms(lane, profile))
    limit = int(cap or getattr(lane, "max_queries", 0) or 0)
    return queries[:limit] if limit else queries


def load_document(path: Path | str, kind: str = "") -> Any:
    """The onboarding document at ``path``, decoded by contract.

    The registry owns this, not the lane and not this module: a kind is decoded
    by the contract registered for it, and an unregistered document reads back as
    the mapping it is. Reaching into a fixed list of three contracts here is what
    made a fourth object impossible.
    """
    from .profiles import load as load_registered

    return load_registered(path, kind)


def load_onboarding_profile(
    workspace: str | Path,
    lane: Any,
    profile_path: str | Path | None = None,
    store: Any = None,
) -> Any:
    """The onboarding profile that supplies this lane's query values.

    The lane names a **kind**, not a filename, and the kind is resolved the way
    the rest of the product resolves profiles: the store's active revision for
    that kind is the durable answer, and the workspace's authoring JSON is what
    creates a revision when none exists yet. That is the whole point of keeping
    immutable revisions — a run can be traced back to the exact onboarding that
    was selected when it started — and reading a loose file instead is what let
    the two drift apart.

    An explicit ``--profile`` wins and is persisted, so the file an operator
    points at becomes the active revision rather than a second source of truth.
    No profile authored yet is not an error: the lane's own terms still supply
    the run.
    """
    root = Path(workspace).expanduser()
    kind = str(getattr(lane, "onboarding_profile", "") or "").strip()
    if profile_path:
        explicit = Path(profile_path).expanduser()
        profile = load_document(explicit if explicit.is_absolute() else root / explicit, kind)
        if profile is not None and store is not None:
            store.save_profile(profile, kind or None)
        return profile
    if not kind:
        return None
    if store is not None:
        active = store.load_profile(kind)
        if active is not None:
            return active
    from .profiles import authoring_file as kind_authoring_file

    authoring = kind_authoring_file(kind)
    profile = load_document(root / authoring, kind) if authoring else None
    if profile is not None and store is not None:
        store.save_profile(profile, kind)
    return profile
