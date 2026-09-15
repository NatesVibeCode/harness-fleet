"""The dossier's evidence readout: kinds present, kinds missing, lone claims."""
from argparse import Namespace

from career_fleet.cli import cmd_dossier
from career_fleet.store import CareerStore


def _dossier(tmp_path, *, description=""):
    store = CareerStore(tmp_path / "evidence.db")
    store.upsert_company("acme", "Acme", domain="acme.example", status="qualified")
    if description:
        store.update_company_description("acme", description) if hasattr(store, "update_company_description") else None
    store.add_job_posting(
        "job-1", "acme", "Staff Kafka Engineer", location="Remote",
        job_url="https://jobs.example/1", raw_text="Own the Kafka platform.",
        source_type="greenhouse",
    )
    return store


def test_dossier_reports_which_evidence_kinds_are_present(capsys, tmp_path):
    _dossier(tmp_path)
    cmd_dossier(Namespace(company="acme", db=str(tmp_path / "evidence.db"), show_source=False))
    out = capsys.readouterr().out
    assert "Evidence kinds present:" in out
    assert "delivery_hiring" in out, "the job posting is ATS evidence"
    assert "Evidence kinds missing:" in out
    assert "independent_validation" in out


def test_dossier_reports_a_claim_that_stands_alone(capsys, tmp_path):
    """A certification only the firm's own material mentions is surfaced."""
    store = CareerStore(tmp_path / "claims.db")
    store.upsert_company("solo", "Solo Consulting", domain="solo.example", status="qualified")
    store.record_evaluation(
        "eval-1", "solo", "lane3_systems", "qualified", 0.8, "HIGH FIT",
        "Certified Premier Partner with a managed services practice.",
        ["Certified Premier Partner.", "We deliver managed services."],
    )
    cmd_dossier(Namespace(company="solo", db=str(tmp_path / "claims.db"), show_source=False))
    out = capsys.readouterr().out
    # A certification only the firm's own material mentions is surfaced, with the
    # missing counterpart named rather than folded into a score.
    assert "Claims that stand alone:" in out
    assert "certification" in out.lower()
