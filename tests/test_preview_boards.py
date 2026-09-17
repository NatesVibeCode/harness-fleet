"""The preview boards are generated, and these are the gates on them.

Every defect these guard against was found by opening the pages in a browser, and
none of them by a test — because nothing checked the two things a generated page
can get wrong: that it is still current, and that the payload it embeds is the
payload the board actually reads. The three pages had drifted into a dialect of
their own (`candidate` for `name`, `quote`/`source_url` for `text`/`source.uri`,
no `facets`), so the evidence view printed "undefined" and the Scorecard view of
every one of them threw.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.build_preview_boards import LANES, OUT_DIR, build_pages, reference_payload

EMBEDDED = re.compile(r"window\.__INITIAL_BOARD_DATA__ = (\{.*?\});?</script>", re.S)
SHAPE_PARTS = ("run", "stats", "ledger")


def _embedded(path: Path) -> dict:
    found = EMBEDDED.search(path.read_text(encoding="utf-8"))
    assert found, f"{path.name} carries no embedded payload"
    return json.loads(found.group(1))


@pytest.fixture(scope="module")
def baked() -> dict[str, str]:
    return build_pages()


@pytest.fixture(scope="module")
def references(tmp_path_factory) -> dict[str, dict]:
    tmp = tmp_path_factory.mktemp("preview-references")
    return {lane: reference_payload(lane, tmp) for lane in LANES}


@pytest.mark.parametrize("lane", sorted(LANES))
def test_committed_page_matches_a_fresh_bake(lane, baked):
    """A page that is not reproducible is a page nobody can review."""
    target = OUT_DIR / f"{lane}-lane.html"
    assert target.read_text(encoding="utf-8") == baked[f"{lane}-lane.html"], (
        "the committed preview is stale; run: python3 tools/build_preview_boards.py"
    )


@pytest.mark.parametrize("lane", sorted(LANES))
def test_embedded_payload_has_the_shape_the_board_is_built_from(lane, references):
    page = _embedded(OUT_DIR / f"{lane}-lane.html")
    live = references[lane]
    assert sorted(page) == sorted(live)
    for part in SHAPE_PARTS:
        assert sorted(page[part]) == sorted(live[part]), part
    assert sorted(page["partners"][0]) == sorted(live["partners"][0])
    assert sorted(page["partners"][0]["quotes"][0]) == sorted(
        live["partners"][0]["quotes"][0]
    )
    assert sorted(page["partners"][0]["source"]) == sorted(live["partners"][0]["source"])
    assert sorted(page["partners"][0]["provenance"]) == sorted(
        live["partners"][0]["provenance"]
    )
    assert sorted(page["checklist"][0]) == sorted(live["checklist"][0])
    assert sorted(page["attributes"][0]) == sorted(live["attributes"][0])


@pytest.mark.parametrize("lane", sorted(LANES))
def test_no_page_speaks_the_retired_fixture_dialect(lane):
    page = _embedded(OUT_DIR / f"{lane}-lane.html")
    for partner in page["partners"]:
        assert partner["name"], "a row without a name renders as its id"
        assert "candidate" not in partner, "the renderer reads `name`"
        assert partner["source"]["uri"], "the evidence view links `source.uri`"
        for quote in partner["quotes"]:
            assert quote["text"], "the evidence view prints `text`"
            assert "quote" not in quote and "source_url" not in quote


@pytest.mark.parametrize("lane", sorted(LANES))
def test_every_attribute_column_has_facets(lane):
    """The scorecard read `p.facets[a.key]` and threw when `facets` was absent."""
    page = _embedded(OUT_DIR / f"{lane}-lane.html")
    assert page["facets"], "a payload with no facets is the crash the bake prevents"
    for attribute in page["attributes"]:
        assert attribute["key"] in page["facets"], attribute["key"]
