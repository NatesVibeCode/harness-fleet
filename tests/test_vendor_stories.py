"""Vendor story enumeration and attribution: stories that name the partner only."""
import pytest

from harness_fleet import enrich
from harness_fleet.enrich import fetch_vendor_stories, vendor_story_urls

# The vendor hubs are shared data now — every lane may need independent prose
# about an entity, not just the partner product — so the shape is the surface
# file's, not a product stage's.
PLAN = {
    "vendor_stories": {
        "vendors": {
            "aws": {"hub": "https://aws.example/partners/success/",
                    "story_path_prefix": "/partners/success/"},
            "vendorx": {"sitemap": "https://vendorx.example/sitemap.xml",
                        "story_paths": ["/customers/", "/case-studies/"]},
        },
    }
}

HUB = """
<html><body>
  <a href="/partners/success/">All stories</a>
  <a href="/de/partners/success/">Deutsch</a>
  <a href="/partners/success/biolytica-presidio/">Biolytica + Presidio</a>
  <a href="/partners/success/clariant-chaos-gears/">Clariant + Chaos Gears</a>
  <a href="/about/">About</a>
</body></html>
"""

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://vendorx.example/sitemap-0.xml</loc></sitemap></sitemapindex>"""

SITEMAP = """<?xml version="1.0"?><urlset>
<url><loc>https://vendorx.example/customers/acme-bank</loc></url>
<url><loc>https://vendorx.example/case-studies/retail-rollout</loc></url>
<url><loc>https://vendorx.example/customers/background-asset-blur</loc></url>
<url><loc>https://vendorx.example/pricing</loc></url>
</urlset>"""


@pytest.fixture
def markup(monkeypatch):
    pages = {
        "https://aws.example/partners/success/": HUB,
        "https://vendorx.example/sitemap.xml": SITEMAP_INDEX,
        "https://vendorx.example/sitemap-0.xml": SITEMAP,
    }
    monkeypatch.setattr(enrich, "fetch_markup", lambda url, **kw: pages[url])
    return pages


def test_hub_enumeration_keeps_stories_and_drops_the_hub_and_locales(markup):
    urls, skipped = vendor_story_urls(PLAN, vendors=["aws"], respect_robots=False)
    assert urls == [
        "https://aws.example/partners/success/biolytica-presidio/",
        "https://aws.example/partners/success/clariant-chaos-gears/",
    ]
    assert skipped == []


def test_sitemap_enumeration_walks_one_level_and_filters_assets(markup):
    urls, skipped = vendor_story_urls(PLAN, vendors=["vendorx"], respect_robots=False)
    assert "https://vendorx.example/customers/acme-bank" in urls
    assert "https://vendorx.example/case-studies/retail-rollout" in urls
    assert "https://vendorx.example/pricing" not in urls, "not a story path"
    assert not any("background" in u for u in urls), "CMS assets are not prose"


def test_unreachable_vendor_is_reported_not_fatal(monkeypatch, markup):
    def boom(url, **kw):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(enrich, "fetch_markup", boom)
    urls, skipped = vendor_story_urls(PLAN, vendors=["aws"], respect_robots=False)
    assert urls == []
    assert skipped and "503" in skipped[0]["reason"]


def test_only_stories_that_name_the_partner_are_kept(monkeypatch, markup):
    from harness_fleet import discover
    from harness_fleet.discover import RawRecord

    pages = {
        "https://aws.example/partners/success/biolytica-presidio/":
            "Biolytica worked with Presidio to ship a data platform.",
        "https://aws.example/partners/success/clariant-chaos-gears/":
            "Clariant and Chaos Gears built a migration practice.",
    }
    monkeypatch.setattr(discover, "fetch_text",
                        lambda url, **kw: RawRecord(text=pages[url], source_uri=url))

    records, skipped = fetch_vendor_stories(
        "presidio.com", sources=PLAN, vendors=["aws"], respect_robots=False,
    )
    assert [r.source_uri for r in records] == [
        "https://aws.example/partners/success/biolytica-presidio/"
    ]


def test_a_story_naming_nobody_is_dropped_not_misfiled(monkeypatch, markup):
    from harness_fleet import discover
    from harness_fleet.discover import RawRecord

    monkeypatch.setattr(discover, "fetch_text", lambda url, **kw: RawRecord(
        text="A customer built something with a vendor.", source_uri=url))

    # The entity is not named in the text, and its name is not in the slug, so
    # there is nothing tying this story to them.
    records, skipped = fetch_vendor_stories(
        "acme.example", sources=PLAN, vendors=["aws"], respect_robots=False,
    )
    assert records == [], "no attribution means no dossier entry"


def test_story_pages_fetch_in_parallel_not_in_sequence(monkeypatch):
    """Eight pages at 0.5s each cannot finish in 2s one at a time."""
    import time

    from harness_fleet import discover
    from harness_fleet.discover import RawRecord

    pages = {
        f"https://aws.example/partners/success/acme-{index}/":
        f"Acme {index} built a platform with Presidio engineers."
        for index in range(8)
    }

    def slow_fetch(url, **kw):
        time.sleep(0.5)
        return RawRecord(text=pages[url], source_uri=url)

    monkeypatch.setattr(discover, "fetch_text", slow_fetch)
    started = time.monotonic()
    records, skipped = fetch_vendor_stories(
        "presidio.com", sources=PLAN, urls=list(pages), respect_robots=False,
    )
    elapsed = time.monotonic() - started
    assert [record.source_uri for record in records] == list(pages), "input order preserved"
    assert skipped == []
    assert elapsed < 2.0, f"8 pages at 0.5s each took {elapsed:.2f}s: fetched in sequence"


def test_vendor_story_records_classify_as_vendor_registry():
    """Vendor-published stories are the non-first-party evidence the gate needs."""
    from harness_fleet.bundler import classify_source_category

    assert classify_source_category(
        "https://aws.amazon.com/partners/success/biolytica-presidio/", "presidio.com",
    ) == "vendor_registry"
    assert classify_source_category(
        "https://www.databricks.com/customers/acme", "trace3.com",
    ) == "vendor_registry"
    assert classify_source_category(
        "https://www.elastic.co/customers/visa", "trace3.com",
    ) == "vendor_registry"
    # A vendor page that is not a story is not registry evidence either.
    assert classify_source_category("https://www.snowflake.com/pricing", "trace3.com") == "general_web"
