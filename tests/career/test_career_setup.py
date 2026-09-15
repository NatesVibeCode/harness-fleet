from career_fleet.setup import install_skill


def test_install_skill_is_idempotent(tmp_path):
    first = install_skill(tmp_path)
    second = install_skill(tmp_path)

    skill = tmp_path / ".agents" / "skills" / "career-fleet"
    assert first["action"] == "created"
    assert second["action"] == "unchanged"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "references" / "iep-interview.md").is_file()
