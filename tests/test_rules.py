import unittest

from src.rules import valid_grant_window
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertTrue(valid_grant_window("2099-01-01", "2026-09-24"))
        self.assertFalse(valid_grant_window("2026-01-01", "2026-09-24"))

    def test_approve_requires_recorded_quorum(self):
        entity = {"kind": "application", "status": "under_review", "data": {}}
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin, entity, "approve", {"terms": "x", "expires_at": "2099-01-01"}
            )

    def test_approve_blocked_by_rejection_even_with_three_approvals(self):
        entity = {
            "kind": "application",
            "status": "under_review",
            "data": {
                "eligible_members": ["m1", "m2", "m3", "m4"],
                "conflicts": [],
                "votes": [
                    {"member": "m1", "voter": "m1", "vote": "approve", "proxy": False},
                    {"member": "m2", "voter": "m2", "vote": "approve", "proxy": False},
                    {"member": "m3", "voter": "m3", "vote": "approve", "proxy": False},
                    {"member": "m4", "voter": "m4", "vote": "reject", "proxy": False},
                ],
            },
        }
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin, entity, "approve", {"terms": "x", "expires_at": "2099-01-01"}
            )

    def test_conflicted_member_can_only_abstain(self):
        entity = {
            "kind": "application",
            "status": "under_review",
            "data": {
                "eligible_members": ["m1", "m2", "m3"],
                "conflicts": ["m2"],
                "proxies": [],
                "votes": [],
            },
        }
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                Actor("m2", "committee"), entity, "vote", {"vote": "approve"}
            )

    def test_non_member_cannot_vote(self):
        entity = {
            "kind": "application",
            "status": "under_review",
            "data": {
                "eligible_members": ["m1", "m2", "m3"],
                "conflicts": [],
                "proxies": [],
                "votes": [],
            },
        }
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                Actor("outsider", "committee"), entity, "vote", {"vote": "approve"}
            )


if __name__ == "__main__":
    unittest.main()
