from career_fleet.setup import install_skill


def test_install_skill_is_idempotent(tmp_path):
    first = install_skill(tmp_path)
    second = install_skill(tmp_path)

    skill = tmp_path / ".agents" / "skills" / "career-fleet"
    assert first["action"] == "created"
    assert second["action"] == "unchanged"
    assert (skill / "SKILL.md").is_file()
    # The lane skill is one file now: the playbooks documented a separate CLI
    # that no longer exists, and the procedure lives in the harness-fleet skill.
    assert not (skill / "references").exists()
