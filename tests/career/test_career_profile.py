import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from career_fleet.profile import IdealEmployerProfile


class TestProfile(unittest.TestCase):
    def test_default_profile(self):
        prof = IdealEmployerProfile()
        self.assertIsNone(prof.dealbreakers.max_headcount)
        self.assertEqual(prof.dealbreakers.policy, "any")
        self.assertFalse(prof.dealbreakers.reject_thin_wrappers)
        self.assertEqual(prof.wedge_capabilities, [])

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p_path = Path(tmpdir) / "test_prof.json"
            prof = IdealEmployerProfile(profile_name="Test Operator")
            prof.save(p_path)
            self.assertTrue(p_path.exists())

            loaded = IdealEmployerProfile.load(p_path)
            self.assertEqual(loaded.profile_name, "Test Operator")
            self.assertIsNone(loaded.dealbreakers.max_headcount)

    def test_unknown_profile_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            IdealEmployerProfile.model_validate({"required_stak": ["Python"]})
        with self.assertRaises(ValidationError):
            IdealEmployerProfile.model_validate({"dealbreakers": {"max_headcont": 10}})

    def test_non_positive_headcount_limit_is_rejected(self):
        with self.assertRaises(ValidationError):
            IdealEmployerProfile.model_validate({"dealbreakers": {"max_headcount": 0}})

    def test_prompt_contains_all_profile_signal_groups(self):
        profile = IdealEmployerProfile(
            required_stack=["Python"],
            negative_stack=["Legacy Mainframe"],
            anchor_companies=["Example Co"],
        )
        prompt = profile.to_evaluation_prompt()
        self.assertIn("Python", prompt)
        self.assertIn("Legacy Mainframe", prompt)
        self.assertIn("Example Co", prompt)
        self.assertIn("HARD DEALBREAKERS", prompt)
