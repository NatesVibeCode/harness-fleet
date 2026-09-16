"""Who a source is about: the company, not the publisher or the platform."""
from __future__ import annotations

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
