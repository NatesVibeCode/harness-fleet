"""Who a source is about: the company, not the publisher or the platform."""
from __future__ import annotations

from harness_fleet.discover import _json_ld_employer
from harness_fleet.evidence import coverage
from harness_fleet.sources import entity_key_for


def test_a_company_service_subdomain_is_the_company():
    """careers.acme.com is acme.com; splitting it made one account look like two."""
    assert entity_key_for("https://careers.toasttab.com/jobs") == "toasttab.com"
    assert entity_key_for("https://jobs.acme.com/1") == "acme.com"
    assert entity_key_for("https://recruiting.example.co.uk/roles") == "example.co.uk"


def test_a_shared_ats_host_still_names_its_account():
    assert entity_key_for("https://jobs.ashbyhq.com/acme") == "acme.com"
    assert entity_key_for("https://boards.greenhouse.io/acme") == "acme.com"


def test_code_platforms_are_not_entities():
    """A repository page that names no company is a library, not an account."""
    assert entity_key_for("https://github.com/foo/bar", "AWS (EC2, RDS, Lambda), Kubernetes") == ""
    assert entity_key_for("https://gitlab.com/foo/bar", "no links") == ""
    assert entity_key_for("https://stackoverflow.com/q/1", "answers") == ""


def test_a_platform_page_that_names_a_company_is_that_company():
    assert entity_key_for("https://github.com/foo/bar", "see https://acme.com/case-studies/x") == "acme.com"


def test_an_empty_board_page_is_not_hiring_evidence():
    """An ATS page that says 'no jobs matching' is an ATS page that says nothing."""
    empty = ("=== SECTION: ATS_REQUISITIONS (URI: https://careers.toasttab.com/) ===\n"
             "There are currently no jobs matching this criterion")
    assert coverage(empty, "https://careers.toasttab.com/")["delivery_hiring"] is False


def test_a_real_posting_is_hiring_evidence():
    posting = ("=== SECTION: ATS_REQUISITIONS (URI: https://boards.greenhouse.io/acme) ===\n"
               "We are hiring a Senior Enterprise Sales Director to own our largest accounts "
               "across the region, working remotely with our field team.")
    assert coverage(posting, "https://boards.greenhouse.io/acme")["delivery_hiring"] is True


POSTING_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Enterprise Sales Director",
 "hiringOrganization":{"@type":"Organization","name":"WorkWave",
                       "url":"https://www.workwave.com"},
 "description":"Own enterprise accounts."}
</script></head><body><h1>Enterprise Sales Director</h1></body></html>
"""


def test_a_syndicated_posting_is_filed_under_the_employer():
    """A board republishing a requisition is not the company doing the hiring."""
    employer, domain = _json_ld_employer(POSTING_HTML)
    assert (employer, domain) == ("WorkWave", "workwave.com")
    key = entity_key_for("https://www.simplyhired.com/job/abc", "the posting text",
                         {"hiring_organization": employer, "hiring_domain": domain})
    assert key == "workwave.com"


def test_an_employer_named_without_a_domain_still_beats_the_board():
    """The page's own statement outranks the host, even with no URL to lean on."""
    html = ('<script type="application/ld+json">'
            '{"@type":"JobPosting","hiringOrganization":{"name":"Acme Robotics"}}'
            '</script>')
    employer, domain = _json_ld_employer(html)
    assert (employer, domain) == ("Acme Robotics", "")
    assert entity_key_for("https://www.glassdoor.com/job/x", "", {
        "hiring_organization": employer, "hiring_domain": domain}) == "Acme Robotics"


def test_a_plain_page_names_no_employer():
    """No structured posting, no invented attribution."""
    assert _json_ld_employer("<html><body>hello</body></html>") == ("", "")
    assert _json_ld_employer(
        '<script type="application/ld+json">{"@type":"Article","headline":"x"}</script>'
    ) == ("", "")
