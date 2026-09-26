import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ACTIVE_WINDOW = {"starts_at": "2020-01-01T00:00:00+00:00", "expires_at": "2099-01-01T00:00:00+00:00"}
EXPIRED_WINDOW = {"starts_at": "2020-01-01T00:00:00+00:00", "expires_at": "2021-01-01T00:00:00+00:00"}


def _proxy(delegator, proxy, window):
    return dict({"delegator": delegator, "proxy": proxy}, **window)


class VotingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _voter(self, user_id):
        return Actor(user_id, "committee")

    def _reviewed_application(self, members=("alice", "bob", "carol", "dave"), proxies=(), conflicts=()):
        committee = self.service.create(self.admin, "committee", {
            "name": "DAC-1",
            "members": list(members),
            "proxies": list(proxies),
        })
        dataset = self.service.create(self.admin, "dataset", {"name": "D", "access_policy": "controlled"})
        application = self.service.create(self.admin, "application", {
            "dataset_id": dataset["id"], "applicant_id": "APP-1", "purpose": "research",
        })
        self.service.transition(self.admin, application["id"], "submit", {})
        application = self.service.transition(self.admin, application["id"], "review", {
            "committee_id": committee["id"], "conflicts": list(conflicts),
        })
        return committee, application

    def _vote(self, application, user_id, ballot, on_behalf_of=None):
        data = {"ballot": ballot}
        if on_behalf_of:
            data["on_behalf_of"] = on_behalf_of
        return self.service.transition(self._voter(user_id), application["id"], "vote", data)

    def _approve(self, application):
        return self.service.transition(
            self.admin, application["id"], "approve",
            {"terms": "noncommercial", "expires_at": "2099-01-01"},
        )

    def test_proxy_vote_records_delegator_and_voter(self):
        _, application = self._reviewed_application(proxies=[_proxy("dave", "erin", ACTIVE_WINDOW)])
        updated = self._vote(application, "erin", "approve", on_behalf_of="dave")
        vote = updated["data"]["votes"][0]
        self.assertEqual(vote["member"], "dave")
        self.assertEqual(vote["voter"], "erin")
        self.assertTrue(vote["proxy"])
        self.assertEqual(vote["ballot"], "approve")

    def test_expired_proxy_window_rejected(self):
        _, application = self._reviewed_application(proxies=[_proxy("dave", "erin", EXPIRED_WINDOW)])
        with self.assertRaises(PermissionDenied):
            self._vote(application, "erin", "approve", on_behalf_of="dave")

    def test_unregistered_proxy_rejected(self):
        _, application = self._reviewed_application()
        with self.assertRaises(PermissionDenied):
            self._vote(application, "erin", "approve", on_behalf_of="dave")

    def test_conflicted_member_must_recuse(self):
        _, application = self._reviewed_application(conflicts=["carol"])
        with self.assertRaises(PermissionDenied):
            self._vote(application, "carol", "approve")
        updated = self._vote(application, "carol", "recuse")
        self.assertEqual(updated["data"]["votes"][0]["ballot"], "recuse")

    def test_conflicted_proxy_seat_must_recuse(self):
        _, application = self._reviewed_application(
            proxies=[_proxy("dave", "erin", ACTIVE_WINDOW)], conflicts=["dave"]
        )
        with self.assertRaises(PermissionDenied):
            self._vote(application, "erin", "approve", on_behalf_of="dave")

    def test_member_votes_only_once(self):
        _, application = self._reviewed_application()
        application = self._vote(application, "alice", "approve")
        with self.assertRaises(ConflictError):
            self._vote(application, "alice", "reject")

    def test_proxy_cannot_double_vote_seat(self):
        _, application = self._reviewed_application(proxies=[_proxy("dave", "erin", ACTIVE_WINDOW)])
        application = self._vote(application, "dave", "approve")
        with self.assertRaises(ConflictError):
            self._vote(application, "erin", "approve", on_behalf_of="dave")

    def test_outsider_cannot_vote(self):
        _, application = self._reviewed_application()
        with self.assertRaises(PermissionDenied):
            self._vote(application, "mallory", "approve")

    def test_invalid_ballot_rejected(self):
        _, application = self._reviewed_application()
        with self.assertRaises(ValidationError):
            self._vote(application, "alice", "maybe")

    def test_insufficient_votes_keep_pending_with_reason(self):
        _, application = self._reviewed_application()
        application = self._vote(application, "alice", "approve")
        application = self._vote(application, "bob", "approve")
        application = self._approve(application)
        self.assertEqual(application["status"], "under_review")
        reason = application["data"]["pending_reason"]
        self.assertIn("2/3", reason)
        self.assertIn("carol", reason)
        self.assertIn("dave", reason)

    def test_three_clean_approvals_approve(self):
        _, application = self._reviewed_application()
        for member in ("alice", "bob", "carol"):
            application = self._vote(application, member, "approve")
        application = self._approve(application)
        self.assertEqual(application["status"], "approved")
        decision = application["data"]["decision"]
        self.assertEqual(decision["approvals"], 3)
        self.assertEqual(decision["rejections"], 0)
        self.assertEqual(application["data"]["pending_reason"], "")

    def test_rejection_blocks_approval(self):
        _, application = self._reviewed_application()
        for member in ("alice", "bob", "carol"):
            application = self._vote(application, member, "approve")
        application = self._vote(application, "dave", "reject")
        application = self._approve(application)
        self.assertEqual(application["status"], "under_review")
        self.assertIn("dave", application["data"]["pending_reason"])

    def test_committee_update_after_votes_keeps_conclusion(self):
        committee, application = self._reviewed_application()
        for member in ("alice", "bob", "carol"):
            application = self._vote(application, member, "approve")
        # 表决后调整委员关系：移除 alice、新增 zoe，不影响本次结论
        self.service.transition(
            self.admin, committee["id"], "update",
            {"members": ["bob", "carol", "dave", "zoe"]},
        )
        application = self._approve(application)
        self.assertEqual(application["status"], "approved")

    def test_new_member_after_review_cannot_vote(self):
        committee, application = self._reviewed_application()
        self.service.transition(
            self.admin, committee["id"], "update",
            {"members": ["alice", "bob", "carol", "dave", "zoe"]},
        )
        with self.assertRaises(PermissionDenied):
            self._vote(application, "zoe", "approve")


class CommitteeRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_members_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "committee", {"members": ["alice", "alice"]})

    def test_proxy_for_non_member_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "committee", {
                "members": ["alice", "bob"],
                "proxies": [_proxy("nobody", "erin", ACTIVE_WINDOW)],
            })

    def test_inverted_proxy_window_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "committee", {
                "members": ["alice", "bob"],
                "proxies": [_proxy("alice", "erin", {"starts_at": "2099-01-01", "expires_at": "2020-01-01"})],
            })

    def test_only_admin_registers_committee(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("carol", "committee"), "committee", {"members": ["alice"]})

    def test_update_cannot_orphan_proxy_delegator(self):
        committee = self.service.create(self.admin, "committee", {
            "members": ["alice", "bob"],
            "proxies": [_proxy("alice", "erin", ACTIVE_WINDOW)],
        })
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, committee["id"], "update", {"members": ["bob"]}
            )


if __name__ == "__main__":
    unittest.main()
