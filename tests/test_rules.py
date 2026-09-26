import unittest

from src.rules import valid_grant_window
from src.domain import Actor
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertTrue(valid_grant_window("2099-01-01", "2026-09-24"))
        self.assertFalse(valid_grant_window("2026-01-01", "2026-09-24"))
        entity = {
            "kind": "application",
            "status": "under_review",
            "data": {
                "panel": {"members": ["a", "b", "c"], "conflicts": [], "proxies": []},
                "votes": [],
            },
        }
        next_status, patch = self.rules.validate_transition(self.admin, entity, "approve", {})
        self.assertEqual(next_status, "under_review")
        self.assertIn("0/3", patch["pending_reason"])


if __name__ == "__main__":
    unittest.main()
