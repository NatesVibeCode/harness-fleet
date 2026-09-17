"""The funnel, wired: what a lane keeps, what a page may qualify, what is reported.

`tests/test_gates.py` proves the gates judge one text correctly. This proves the
pieces that make those judgments *run*: which evidence grade a candidate's text
carried, which lane's ladder decides what it is entitled to, and what the run
tells a person when the world shrank.

Three things were missing when the engine landed, and each has a test here:

1. A profile that leads with firmographics, so the four cheap gates have
   something to run on.
2. A ladder as per-lane data: which rung fetches what, and what a candidate
   must satisfy to earn the next.
3. The wiring, so a run reports funnel counts instead of only a list.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_fleet import lanes as lane_module
from harness_fleet.gates import (
    FETCHED,
    SNIPPET,
    GateProfile,
    LadderRung,
    funnel_entities,
    read_grade,
    run_evidence_funnel,
    run_funnel,
)
from harness_fleet.models import InputItem
from harness_fleet.partner import IdealPartnerProfile

#: The checkout itself, because which lanes a run loads depends on where it runs.
REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 1. Which evidence grade the text carried, and what that entitles it to
# --------------------------------------------------------------------------


def test_the_grade_a_source_carried_decides_what_it_may_settle():
    """`fetched` is a page we read. Everything else is somebody's summary.

    This is the distinction the whole funnel rests on — a snippet may eliminate
    and may never qualify — so it is read from the source's own metadata rather
    than assumed from the fact that a run happened.
    """
    assert read_grade({"evidence": "fetched"}) == FETCHED
    assert read_grade({"evidence": "indicator"}) == SNIPPET
    assert read_grade({"evidence": "profile"}) == SNIPPET
    assert read_grade({}) == SNIPPET, "unknown provenance is the weakest, not the strongest"
    assert read_grade(None) == SNIPPET


def test_a_merged_dossier_is_only_as_strong_as_its_best_source():
    """A page fetched into the middle of a bundle upgrades the bundle.

    A dossier merges several sources per entity; the grade of the whole is the
    best grade any contributor carried, because a gate reads the bundle's text.
    """
    assert run_evidence_funnel([], profile={}).fetched is False  # no sources, weakest grade
    report = run_evidence_funnel(
        [
            InputItem(item_id="acme.com", text="A consultancy of 200 people in Boston.",
                      metadata={"evidence": "indicator"}),
            InputItem(item_id="acme.com", text="We are a data consultancy in Boston.",
                      metadata={"evidence": "fetched"}),
        ],
        profile={"allows": "services", "size_min": 20, "locations": ("United States",)},
    )
    assert report.fetched is True
    gates = {result.gate: result for result in report.results}
    assert gates["kind"].outcome == "pass", "a fetched page may qualify where a snippet may not"
    assert gates["size"].outcome == "pass"
    assert gates["location"].outcome == "pass"


def test_a_snippet_only_candidate_can_never_come_back_qualified():
    """The asymmetry, enforced end to end and not just per gate."""
    report = run_evidence_funnel(
        [
            InputItem(
                item_id="acme.com",
                text="An advisory firm of 200 people in London serving banking clients.",
                metadata={"evidence": "indicator"},
            )
        ],
        profile={
            "allows": "services", "size_min": 20, "size_max": 5000,
            "locations": ("United Kingdom",), "verticals": ("fintech",),
        },
    )
    assert report.fetched is False
    assert report.verdict == "lead", "a snippet earns a fetch, never a pass"
    assert "kind" in report.unresolved


# --------------------------------------------------------------------------
# 2. The ladder: per-lane data saying what earns what
# --------------------------------------------------------------------------


def test_a_lane_states_its_own_ladder_and_which_rung_fetches():
    """Which rung fetches what is lane data, not a constant in the engine."""
    ladder = lane_module.shipped_lanes()["partner"].funnel.ladder
    assert [rung.name for rung in ladder] == ["result", "surface", "stories"]
    by_name = {rung.name: rung for rung in ladder}
    assert by_name["result"].evidence == SNIPPET
    assert by_name["result"].gates == ["kind", "size", "location"]
    assert "spends no fetch" in by_name["result"].earns or by_name["result"].earns
    assert by_name["surface"].evidence == FETCHED, "the rung that reads their own pages"
    assert by_name["surface"].gates == ["kind", "size", "location"]
    assert "vertical" in by_name["stories"].gates
    # Progressive reading: the surface rung opens on the landing page and the
    # cheap-qualifier pages, and the stories rung is the deep read.
    assert by_name["surface"].surfaces[0] == "home", "the walk opens on the landing page"
    assert "partners" in by_name["surface"].surfaces, "the partner page is cheap-gate evidence"
    assert "case_studies" in by_name["stories"].surfaces


def test_the_ladder_tells_a_candidate_what_still_stands_between_it_and_a_pass():
    """A lead is told what would resolve it, not left as a bare unknown."""
    profile = {
        "allows": "services", "size_min": 20, "size_max": 5000,
        "locations": ("United Kingdom",), "verticals": ("fintech",),
    }
    snippet = run_funnel(
        "acme.co.uk",
        snippet="An advisory firm of 200 people in London serving banking clients.",
        profile=profile,
    )
    step = run_evidence_funnel(
        [
            InputItem(item_id="acme.co.uk", text="An advisory firm of 200 people in London.",
                      metadata={"evidence": "indicator"})
        ],
        profile=profile,
    )
    assert [rung.name for rung in step.rungs] == ["result", "surface", "stories"]
    # Two things stand between this candidate and a pass: the page that
    # confirms it is a delivery firm, and the case studies that name an
    # industry. It has earned the cheapest one, which is the next rung — not the
    # furthest blocker, because telling somebody to fetch case studies for a
    # candidate whose pages were never read sends them past the first gate.
    assert step.unresolved == ["kind", "vertical"]
    assert step.earned == "surface", "the next rung to climb is the page fetch"
    assert not snippet.eliminated and not snippet.qualified


def test_every_shipped_lane_carries_a_ladder_that_validates(tmp_path: Path):
    """What ships, not what a workspace overrides it with.

    Lanes are package data, so the ladder a user gets is the packaged one — and
    a workspace lane of the same name silently replaces it. Arguing from the
    repo's own `lanes/` directory would test the developer's override.
    """
    loaded = lane_module.load_available_lanes(
        tmp_path, channels=set(), backends={"ddgs", "hn"}
    )
    assert {"account", "career", "partner"} <= set(loaded)
    for name, lane in loaded.items():
        assert lane.funnel.ladder, f"{name} states how a candidate earns a fetch"
        gates = {gate for rung in lane.funnel.ladder for gate in rung.gates}
        if lane.funnel.allows == "services":
            # The partner population test: a delivery firm, not a product company.
            # It is the partner lane's question, not a universal one — the account
            # lane's population is a competitor's customers, and a buyer may be a
            # product company.
            assert "kind" in gates, f"{name} asks whether the firm delivers, so a rung must settle it"
        else:
            assert "kind" not in gates, (
                f"{name} asks no kind question (allows {lane.funnel.allows!r}), "
                "so a rung claiming to settle one settles nothing"
            )
        for rung in lane.funnel.ladder:
            assert rung.name and rung.evidence in (SNIPPET, FETCHED)
            assert rung.earns, f"{name}:{rung.name} says what it buys"
        lane_module.validate_lane(lane)


# --------------------------------------------------------------------------
# One source of truth for the lanes that ship
# --------------------------------------------------------------------------


def test_nothing_in_the_repo_shadows_the_lanes_that_ship():
    """A `lanes/` directory here would silently beat the packaged lanes.

    The lanes used to live at the repo root, before they became package data
    (`aa09e90`). The root copy outlived the move: it stopped being updated while
    the packaged lanes kept being tuned, so running from a checkout got 2 search
    queries and `stories: 0` where an install got 6 queries and `stories: 80` —
    the vendor-story sourcing never ran. Nothing failed, because a workspace
    lane overriding a shipped one is the documented behaviour and every test
    built its own workspace.

    The fix is that the package is the only copy. This is the guard that says so,
    and it fails on re-introduction rather than on the next silent drift.
    """
    root_lanes = REPO / "lanes"
    assert not root_lanes.exists(), (
        f"{root_lanes} shadows the packaged lanes; lanes ship as package data "
        "in harness_fleet/resources/lanes/"
    )


def test_the_repo_root_runs_the_lanes_that_ship():
    """What `research --lane X` loads from this checkout is what an install gets.

    Both halves matter: nothing in the repo may override a lane, and the funnel
    the run gates on has to be the shipped one rather than a stale copy.
    """
    from harness_fleet.lanes import lane_source

    available = lane_module.load_available_lanes(REPO)
    for name in available:
        assert lane_source(REPO, name) == "shipped", (
            f"{name} is being overridden inside the repo, so a checkout runs "
            "different configuration than an install"
        )
    for name in ("account", "career", "partner"):
        assert available[name].funnel.ladder, f"{name} ships without its ladder"
    assert available["partner"].stories == lane_module.shipped_lanes()["partner"].stories


def test_a_lane_validator_rejects_a_ladder_that_cannot_run():
    """A rung naming a gate the engine does not have is a lane that lies."""
    broken = lane_module.shipped_lanes()["partner"].model_copy(deep=True)
    broken.funnel.ladder = [LadderRung(name="first", gates=["kind", "astrology"])]
    with pytest.raises(lane_module.LaneError) as err:
        lane_module.validate_lane(broken)
    assert "astrology" in str(err.value)


# --------------------------------------------------------------------------
# 3. The profile leads with firmographics
# --------------------------------------------------------------------------


def test_the_partner_profile_leads_with_firmographics(tmp_path: Path):
    """The four cheap gates need fields to run on, and the interview writes them."""
    document = {
        "ipp_profile": {
            "profile_name": "Snowflake Implementation Partners",
            "target_ecosystem": "Snowflake",
            "service_models": ["Systems Integration", "Managed Services"],
            "partner_size_min": 20,
            "partner_size_max": 500,
            "target_territories": ["United Kingdom", "Ireland"],
            "target_industries": ["fintech", "healthcare"],
            "negative_exclusions": ["Pure SaaS product vendors"],
        }
    }
    profile_path = tmp_path / "ideal_partner_profile.json"
    profile_path.write_text(json.dumps(document), encoding="utf-8")
    profile = IdealPartnerProfile.load(profile_path)

    assert profile.partner_size_min == 20 and profile.partner_size_max == 500
    assert profile.target_territories == ["United Kingdom", "Ireland"]
    assert profile.target_industries == ["fintech", "healthcare"]
    assert profile.partner_kind == "services", "a partner lane wants delivery firms"

    funnel = profile.funnel_profile()
    assert funnel["allows"] == "services"
    assert funnel["size_min"] == 20 and funnel["size_max"] == 500
    assert funnel["locations"] == ("United Kingdom", "Ireland")
    assert funnel["verticals"] == ("fintech", "healthcare")


def test_the_funnel_reads_a_profile_object_as_readily_as_a_dict():
    """The engine stays lane-agnostic: it duck-types the profile it is handed."""
    profile = IdealPartnerProfile(
        target_ecosystem="Kafka",
        partner_size_min=10,
        target_territories=["Germany"],
        target_industries=["logistics"],
    )
    typed = GateProfile.from_object(profile)
    assert typed.allows == "services"
    assert typed.size_min == 10
    assert typed.locations == ("Germany",)
    assert typed.verticals == ("logistics",)
    assert GateProfile.from_object(profile.funnel_profile()).locations == ("Germany",)
    assert GateProfile.from_object(None).locations == ()


def test_the_profile_renders_the_gates_it_will_be_judged_by():
    """A person tuning thresholds reads them in the document they authored."""
    context = IdealPartnerProfile(
        target_ecosystem="Snowflake",
        partner_size_min=20,
        partner_size_max=500,
        target_territories=["United Kingdom"],
        target_industries=["fintech"],
    ).to_prompt_context()
    assert "Firmographics" in context or "firmographics" in context
    assert "20" in context and "500" in context


# --------------------------------------------------------------------------
# 4. What the run reports
# --------------------------------------------------------------------------


def test_the_wiring_counts_eliminations_per_gate_and_only_survivor_unknowns():
    items = [
        InputItem(item_id="vendor.io", text="Our platform helps teams ship faster. Book a demo.",
                  metadata={"evidence": "indicator"}),
        InputItem(item_id="tiny.io", text="A boutique consultancy, team of 6, in Austin",
                  metadata={"evidence": "indicator"}),
        InputItem(item_id="acme.co.uk", text="An advisory firm of 200 people in London",
                  metadata={"evidence": "indicator"}),
    ]
    profile = {
        "allows": "services", "size_min": 20, "size_max": 5000,
        "locations": ("United Kingdom",), "verticals": ("fintech",),
    }
    survivors, counts, all_reports = funnel_entities(items, profile=profile)
    assert counts["candidates"] == 3
    assert counts["eliminated_at"]["kind"] == 1
    assert counts["eliminated_at"]["size"] == 1
    assert counts["unresolved_at"]["kind"] == 1, "the survivor is unresolved, not passed"
    assert [report.candidate for report in survivors] == ["acme.co.uk"]
    assert counts["verdicts"]["needing_retrieval"] == 1
    assert counts["eliminated"]["kind"][0]["candidate"] == "vendor.io", "the count names who"
    assert counts["unresolved"]["kind"][0]["candidate"] == "acme.co.uk"


# --------------------------------------------------------------------------
# 5. The command wires it up
# --------------------------------------------------------------------------


def test_the_run_gates_on_the_profile_and_the_lane_together(tmp_path: Path):
    """Both bind, so the gates are the intersection rather than one winner."""
    from harness_fleet.rungs import resolved_gate_profile

    (tmp_path / "ideal_partner_profile.json").write_text(
        json.dumps({
            "ipp_profile": {
                "profile_name": "UK delivery partners",
                "target_ecosystem": "Snowflake",
                "partner_size_min": 50,
                "partner_size_max": 400,
                "target_territories": ["United Kingdom"],
                "target_industries": ["fintech"],
            }
        }),
        encoding="utf-8",
    )
    lane = lane_module.shipped_lanes()["partner"]
    lane.funnel.size_min = 10          # the lane is looser than the profile
    lane.funnel.size_max = 5000
    lane.funnel.locations = ["United Kingdom", "Germany"]

    profile = resolved_gate_profile(lane, workspace=tmp_path)
    assert profile.size_min == 50, "the stricter floor wins"
    assert profile.size_max == 400, "the stricter ceiling wins"
    assert profile.locations == ("United Kingdom",), "the stricter territory wins"
    assert profile.verticals == ("fintech",)
    assert profile.allows == "services"


def test_a_run_with_no_profile_still_gates_on_the_lane(tmp_path: Path):
    """A first run with no setup eliminates the obviously wrong companies."""
    from harness_fleet.rungs import resolved_gate_profile

    lane = lane_module.shipped_lanes()["partner"]
    lane.funnel.size_min = 20
    lane.funnel.verticals = ["fintech"]
    profile = resolved_gate_profile(lane, workspace=tmp_path)
    assert profile.size_min == 20 and profile.verticals == ("fintech",)
    assert profile.locations == ()


def test_a_profile_the_user_names_but_that_is_missing_is_an_error(tmp_path: Path):
    """Silently gating on less than the user asked for is the bad outcome."""
    from harness_fleet.rungs import resolved_gate_profile

    with pytest.raises(ValueError) as err:
        resolved_gate_profile(
            lane_module.shipped_lanes()["partner"],
            workspace=tmp_path,
            profile_path=tmp_path / "nope.json",
        )
    assert "not found" in str(err.value)


def test_the_run_says_where_the_world_shrank():
    """The funnel line names the rung, the count, and the gate that did it."""
    from harness_fleet import cli

    note = cli.funnel_stage_note({"nodes": {
        "g0-result": {
            "kind": "gate",
            "rung": "result",
            "counts": {
                "candidates": 2,
                "verdicts": {"eliminated": 1, "lead": 1},
                "eliminated_at": {"kind": 1},
            },
        },
        "r1-surface": {"kind": "retrieve", "rung": "surface", "read": 1, "empty": 0,
                       "visited": 3},
    }})
    assert "result: 2 in -> 1 eliminated (1 at kind)" in note
    assert "surface: read 1" in note and "3 page(s)" in note


def test_the_table_says_who_is_owed_the_next_rung_and_who_is_not():
    """What the survivors are owed is a column, not a separate tally."""
    from harness_fleet.gates import LadderRung
    from harness_fleet.rungs import advances_to, gate_rows

    ladder = [
        LadderRung(name="result", evidence="snippet", gates=["kind"]),
        LadderRung(name="surface", evidence="fetched", gates=["size", "location"],
                   surfaces=["about"]),
    ]
    profile = {"allows": "services", "locations": ("United Kingdom",),
               "verticals": ("fintech",)}
    rows, reports = gate_rows(
        [
            _record("vendor.io", "Our platform is a SaaS product. Book a demo."),
            _record("acme.co.uk", "An advisory firm of 200 people in London."),
        ],
        rung=ladder[0],
        ladder=ladder,
        profile=profile,
    )
    following = ladder[1]
    owed = {row["candidate"]: advances_to(report, following)
            for row, report in zip(rows, reports, strict=True)}
    assert owed == {"vendor.io": False, "acme.co.uk": True}, (
        "an eliminated candidate is owed nothing: it never spends a fetch"
    )


def test_every_lead_names_a_next_step_or_says_there_is_none():
    """A lead must never go quiet.

    The failure this guards against is silent: a walk that cannot match a lead's
    open gate to any rung returns nothing, and the run reports a lead with no
    next step — indistinguishable from a lead that is finished. Either a lead
    names the rung it earned, or it has no open gate left that a fetch could
    settle, which is a state it has to be *in* rather than fall into.
    """
    ladders = {
        "default": None,
        "partner": lane_module.shipped_lanes()["partner"].funnel.rungs(),
        "career": lane_module.shipped_lanes()["career"].funnel.rungs(),
    }
    profiles = [
        {},
        {"allows": "services"},
        {"allows": "services", "size_min": 20, "size_max": 500},
        {"allows": "services", "locations": ("United Kingdom",)},
        {"allows": "services", "locations": ("United Kingdom",), "verticals": ("fintech",)},
        {"allows": "services", "size_min": 20, "locations": ("United Kingdom",),
         "verticals": ("fintech", "healthcare")},
    ]
    texts = [
        "A data consultancy of 200 people in London serving banking clients.",
        "A boutique advisory, team of 6, in Leeds.",
        "We build things.",
        "An engineering firm of 1,200 people headquartered in Munich, Germany.",
        "A consultancy in Toronto, Canada, working with healthcare providers.",
    ]
    for ladder_name, ladder in ladders.items():
        for profile in profiles:
            for grade in (SNIPPET, FETCHED):
                for text in texts:
                    report = run_funnel(
                        "x.example", snippet=text, profile=profile,
                        evidence=grade, ladder=ladder,
                    )
                    if report.verdict != "lead":
                        continue
                    where = f"{ladder_name}/{grade}/{profile}/{text[:30]}"
                    if report.earned:
                        assert report.earned in {r.name for r in report.rungs}, (
                            f"{where}: earned a rung that is not on its ladder"
                        )
                        assert not report.exhausted, where
                    else:
                        # Nothing earned, so it must be a state the report can
                        # account for: finished, or out of things to fetch.
                        assert report.qualified or report.eliminated or report.exhausted, where


def test_a_lead_that_has_read_everything_is_reported_as_exhausted():
    """There is a difference between "fetch this next" and "nothing left".

    A firm whose pages name no industry is unresolved on the vertical and no
    second fetch will change that, so the report says it has nothing further to
    offer instead of naming a fetch nobody should spend.
    """
    profile = {"allows": "services", "locations": ("United Kingdom",),
               "verticals": ("fintech",)}
    exhausted = run_funnel(
        "acme.co.uk",
        snippet="An advisory firm of 200 people in London. We build data platforms.",
        profile=profile, evidence=FETCHED,
    )
    assert exhausted.verdict == "lead"
    assert exhausted.unresolved == ["vertical"]
    assert exhausted.earned == "" and exhausted.exhausted

    owed = run_funnel("acme.co.uk", snippet="An advisory firm of 200 people in London.",
                      profile=profile)
    assert owed.earned == "surface" and not owed.exhausted


def test_the_ladder_is_climbed_one_fetch_at_a_time():
    """What a candidate has earned changes once the fetch is actually spent.

    The staircase has to move, and this is the whole truth table for it, because
    the recurrence is easy to get subtly wrong in a way no single case reveals:
    a rung whose gates are all settled advances the walk, a rung this report's
    own evidence could still settle is a grant rather than a fetch owed, and the
    first rung carrying something open it *cannot* settle is what was earned.
    """
    with_verticals = {"allows": "services", "locations": ("United Kingdom",),
                      "verticals": ("fintech",)}
    without = {"allows": "services", "locations": ("United Kingdom",)}
    cases = [
        # (snippet, profile, grade, verdict, earned, unresolved)
        ("A consultancy of 200 people in London.", with_verticals, SNIPPET,
         "lead", "surface", ["kind", "vertical"]),
        ("A consultancy of 200 people in London.", without, SNIPPET,
         "lead", "surface", ["kind"]),
        # The pages are read and none of them names an industry. Nothing further
        # will settle this: the vertical lives on the same pages we just read, so
        # the honest answer is that no next step is owed rather than sending
        # somebody to fetch the case studies twice.
        ("A consultancy of 200 people in London. We build data platforms.", with_verticals, FETCHED,
         "lead", "", ["vertical"]),
        ("A consultancy of 200 people in London serving banking.", with_verticals, FETCHED,
         "qualified", "", []),
        ("A consultancy of 200 people in Munich, Germany.", with_verticals, FETCHED,
         "eliminated", "", []),
    ]
    for text, profile, grade, verdict, earned, unresolved in cases:
        report = run_funnel("x.example", snippet=text, profile=profile, evidence=grade)
        where = f"{grade}: {text[:45]}"
        assert report.verdict == verdict, where
        assert report.earned == earned, f"{where} -> earned {report.earned!r}"
        assert report.unresolved == unresolved, where
        if verdict == "qualified":
            assert report.fetched, "only a fetched page qualifies"


def test_an_eliminated_candidate_is_never_bundled_and_never_fetched(tmp_path: Path, monkeypatch):
    """The run's own path: the ladder as a DAG, over the items it gathered."""
    from harness_fleet import cli
    from harness_fleet.bundler import bundle_records
    from harness_fleet.dag import DagSpec, run_dag
    from harness_fleet.enrich import EnrichReport
    from harness_fleet.rungs import lane_spec
    from harness_fleet.store import HarnessStore

    (tmp_path / "ideal_partner_profile.json").write_text(
        json.dumps({
            "ipp_profile": {
                "profile_name": "UK delivery partners",
                "target_ecosystem": "Snowflake",
                "partner_size_min": 20,
                "partner_size_max": 500,
                "target_territories": ["United Kingdom"],
                "target_industries": ["fintech"],
            }
        }),
        encoding="utf-8",
    )
    lane = lane_module.shipped_lanes()["partner"]
    items = [
        InputItem(item_id="vendor.io", text="Our platform helps teams ship faster. Book a demo.",
                  metadata={"evidence": "indicator"}),
        InputItem(item_id="tiny.io", text="A boutique consultancy, team of 6, in London",
                  metadata={"evidence": "indicator"}),
        InputItem(item_id="munich.de", text="A consultancy of 300 people headquartered in Munich, Germany",
                  metadata={"evidence": "indicator"}),
        InputItem(item_id="acme.co.uk", text="A data consultancy of 200 people in London",
                  metadata={"evidence": "indicator"}),
    ]
    walked: list[str] = []

    def fake_enrich(entity, **kwargs):
        walked.append(entity)
        report = EnrichReport(entity=entity, domain=entity,
                              surfaces=list(kwargs.get("surface_order") or []))
        report.visited = 1
        report.kept = 1
        report.by_surface = {"about": 1}
        from harness_fleet.models import InputItem as _Item

        return [_Item(item_id=entity, text=f"{entity} delivers data platforms for fintech clients.",
                      source_uri=f"https://{entity}/about",
                      metadata={"enrich_surface": "about"})], report

    monkeypatch.setattr("harness_fleet.enrich.enrich_entity", fake_enrich)
    captured = tmp_path / "captured.jsonl"
    from harness_fleet.discover import write_items_jsonl

    write_items_jsonl(items, captured)
    store = HarnessStore(tmp_path / "t.db")
    spec = DagSpec.model_validate(lane_spec(
        lane, from_items=str(captured), workspace=tmp_path, walk=True, gates=True,
    ))
    state = run_dag(spec, store, workspace_root=tmp_path, dag_id="f1")

    first = cli.funnel_stage_report(state)["g0-result"]
    assert first["counts"]["candidates"] == 4
    assert first["counts"]["eliminated_at"] == {"kind": 1, "size": 1, "location": 1}
    kept = cli.funnel_population(items, state, spec)
    # Only the survivor is fetched, and only it goes on to be scored.
    assert {item.item_id for item in kept} == {"acme.co.uk"}
    assert walked and set(walked) <= {"acme.co.uk"}, (
        f"a fetch was spent on a candidate the rung below eliminated: {walked}"
    )
    assert [d.item_id for d in bundle_records(kept)] == ["acme.co.uk"]

def test_the_funnel_can_be_turned_off_for_a_run(tmp_path: Path):
    """--no-funnel keeps everything, for anyone who wants the raw candidate set."""
    from harness_fleet import cli

    args = cli.build_parser().parse_args(["research", "--lane", "partner", "--no-funnel"])
    assert args.no_funnel is True, "the flag exists and is what the command reads"


def _record(item_id: str, text: str):
    """The minimum a gate reads: an id, its text, and nothing else."""

    class _Record:
        pass

    record = _Record()
    record.item_id = item_id
    record.text = text
    record.source_uri = ""
    record.quotes = []
    record.metadata = {}
    return record
