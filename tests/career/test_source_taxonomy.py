"""Career's dossier labels are the fleet-wide taxonomy, not career-local strings.

Every fleet judges sources the same way; this pins career's dossier sections to
the shared classifier so a label cannot drift into a career-only vocabulary that
the evidence rules no longer understand.
"""
from argparse import Namespace

from career_fleet.cli import _dossier_evidence_text
from harness_fleet.evidence import coverage, parse_sections
from harness_fleet.sources import classify_source_category
from tests.career.test_dossier_evidence import _dossier  # the same fixture shape


def test_the_classifier_agrees_with_the_labels_career_emits():
    """A Greenhouse posting and a Reddit thread classify as career labels them."""
    assert classify_source_category("https://boards.greenhouse.io/acme/jobs/123") == "ats_requisitions"
    assert classify_source_category("https://www.reddit.com/r/devops/comments/x/y") == (
        "community_and_social"
    )
    assert classify_source_category("https://acme.example/case-studies/bank") == (
        "first_party_case_study"
    )


def test_every_section_career_emits_is_a_known_category(tmp_path):
    """No section may carry a category the shared vocabulary does not define."""
    store = _dossier(tmp_path, description="Acme builds Kafka platforms.")
    dossier = store.get_company_dossier("acme")
    text = _dossier_evidence_text(dossier)
    sections = parse_sections(text)
    assert sections, "the dossier must produce labelled sections"
    known = {
        classify_source_category("https://boards.greenhouse.io/acme/jobs/1"),
        classify_source_category("https://www.reddit.com/r/x/comments/a/b"),
        classify_source_category("https://acme.example/case-studies/one"),
        "first_party_practice",
    }
    for section in sections:
        assert section["category"].lower() in known | {"unknown"}, section["category"]
        assert section["uri"], "a section without a source is unattributable"


def test_a_labelled_dossier_reads_as_hiring_evidence(tmp_path):
    """The point of labelling: a job posting becomes delivery_hiring evidence."""
    store = _dossier(tmp_path, description="Acme builds Kafka platforms for banks.")
    dossier = store.get_company_dossier("acme")
    kinds = coverage(_dossier_evidence_text(dossier))
    assert kinds["delivery_hiring"] is True
    assert kinds["independent_validation"] is False, "nothing third-party was captured yet"


def test_the_dossier_command_prints_that_readout(tmp_path, capsys):
    from career_fleet.cli import cmd_dossier

    _dossier(tmp_path, description="Acme builds Kafka platforms for banks.")
    cmd_dossier(Namespace(company="acme", db=str(tmp_path / "evidence.db"), show_source=False))
    out = capsys.readouterr().out
    assert "delivery_hiring" in out
