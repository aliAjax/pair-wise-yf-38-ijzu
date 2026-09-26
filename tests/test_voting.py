import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")
ACTIVE_PROXY = {"starts_at": "2020-01-01", "expires_at": "2099-01-01"}
EXPIRED_PROXY = {"starts_at": "2020-01-01", "expires_at": "2020-12-31"}


def member(user_id):
    return Actor(user_id, "committee")


class VotingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_application(self, members, conflicts=None, proxies=None):
        dataset = self.service.create(
            ADMIN, "dataset", {"name": "D", "access_policy": "controlled"}
        )
        committee_data = {"name": "DAC", "members": members}
        if conflicts:
            committee_data["conflicts"] = conflicts
        if proxies:
            committee_data["proxies"] = proxies
        committee = self.service.create(ADMIN, "committee", committee_data)
        application = self.service.create(
            ADMIN,
            "application",
            {"dataset_id": dataset["id"], "applicant_id": "APP-1", "purpose": "study"},
        )
        self.service.transition(ADMIN, application["id"], "submit", {})
        under_review = self.service.transition(
            ADMIN, application["id"], "review", {"committee_id": committee["id"]}
        )
        return committee, under_review

    def _vote(self, application_id, user_id, vote, on_behalf_of=None):
        data = {"vote": vote}
        if on_behalf_of:
            data["on_behalf_of"] = on_behalf_of
        return self.service.transition(member(user_id), application_id, "vote", data)

    def test_review_locks_eligible_members(self):
        _, application = self._setup_application(["m1", "m2", "m3"], conflicts=["m3"])
        data = application["data"]
        self.assertEqual(data["eligible_members"], ["m1", "m2", "m3"])
        self.assertEqual(data["conflicts"], ["m3"])
        self.assertEqual(data["votes"], [])
        self.assertIn("missing 3 conflict-free approval(s)", data["pending_reason"])

    def test_quorum_reached_allows_approval(self):
        _, application = self._setup_application(["m1", "m2", "m3", "m4"])
        app_id = application["id"]
        self._vote(app_id, "m1", "approve")
        self._vote(app_id, "m2", "approve")
        self._vote(app_id, "m3", "abstain")
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
            )
        self.assertIn("missing 1 conflict-free approval(s)", str(ctx.exception))
        last = self._vote(app_id, "m4", "approve")
        self.assertIsNone(last["data"]["pending_reason"])
        self.assertTrue(last["data"]["tally"]["quorum_met"])
        approved = self.service.transition(
            ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
        )
        self.assertEqual(approved["status"], "approved")

    def test_rejection_blocks_approval_and_explains(self):
        _, application = self._setup_application(["m1", "m2", "m3", "m4"])
        app_id = application["id"]
        self._vote(app_id, "m1", "approve")
        self._vote(app_id, "m2", "approve")
        self._vote(app_id, "m3", "approve")
        last = self._vote(app_id, "m4", "reject")
        self.assertEqual(last["status"], "under_review")
        self.assertIn("blocked by 1 rejection vote(s)", last["data"]["pending_reason"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
            )

    def test_one_vote_per_seat(self):
        _, application = self._setup_application(["m1", "m2", "m3"])
        app_id = application["id"]
        self._vote(app_id, "m1", "approve")
        with self.assertRaises(ConflictError):
            self._vote(app_id, "m1", "reject")

    def test_conflicted_member_can_only_abstain(self):
        _, application = self._setup_application(["m1", "m2", "m3"], conflicts=["m2"])
        app_id = application["id"]
        with self.assertRaises(PermissionDenied):
            self._vote(app_id, "m2", "approve")
        updated = self._vote(app_id, "m2", "abstain")
        self.assertEqual(updated["data"]["tally"]["abstentions"], 1)

    def test_proxy_vote_records_delegator_and_voter(self):
        proxies = [dict(delegator="m1", proxy="m2", **ACTIVE_PROXY)]
        _, application = self._setup_application(["m1", "m2", "m3"], proxies=proxies)
        app_id = application["id"]
        updated = self._vote(app_id, "m2", "approve", on_behalf_of="m1")
        record = updated["data"]["votes"][0]
        self.assertEqual(record["member"], "m1")
        self.assertEqual(record["voter"], "m2")
        self.assertTrue(record["proxy"])
        with self.assertRaises(ConflictError):
            self._vote(app_id, "m2", "approve", on_behalf_of="m1")

    def test_delegating_member_cannot_vote_directly(self):
        proxies = [dict(delegator="m1", proxy="m2", **ACTIVE_PROXY)]
        _, application = self._setup_application(["m1", "m2", "m3"], proxies=proxies)
        with self.assertRaises(PermissionDenied):
            self._vote(application["id"], "m1", "approve")

    def test_expired_proxy_delegation_is_rejected(self):
        proxies = [dict(delegator="m1", proxy="m2", **EXPIRED_PROXY)]
        _, application = self._setup_application(["m1", "m2", "m3"], proxies=proxies)
        with self.assertRaises(PermissionDenied):
            self._vote(application["id"], "m2", "approve", on_behalf_of="m1")

    def test_proxy_must_be_registered_for_delegator(self):
        _, application = self._setup_application(["m1", "m2", "m3"])
        with self.assertRaises(PermissionDenied):
            self._vote(application["id"], "m2", "approve", on_behalf_of="m1")

    def test_amend_after_review_does_not_change_locked_roster(self):
        committee, application = self._setup_application(["m1", "m2", "m3"])
        app_id = application["id"]
        self.service.transition(
            ADMIN, committee["id"], "amend", {"members": ["m9", "m8", "m7"]}
        )
        self._vote(app_id, "m1", "approve")
        self._vote(app_id, "m2", "approve")
        self._vote(app_id, "m3", "approve")
        approved = self.service.transition(
            ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["eligible_members"], ["m1", "m2", "m3"])

    def test_amend_after_approval_does_not_change_conclusion(self):
        committee, application = self._setup_application(["m1", "m2", "m3"])
        app_id = application["id"]
        for user_id in ("m1", "m2", "m3"):
            self._vote(app_id, user_id, "approve")
        self.service.transition(
            ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
        )
        self.service.transition(
            ADMIN,
            committee["id"],
            "amend",
            {"members": ["x1"], "conflicts": [], "proxies": []},
        )
        concluded = self.service.get(app_id)
        self.assertEqual(concluded["status"], "approved")
        self.assertEqual(len(concluded["data"]["votes"]), 3)
        self.assertTrue(concluded["data"]["tally"]["quorum_met"])

    def test_committee_roster_validation(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                ADMIN, "committee", {"name": "bad", "members": ["m1", "m1"]}
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                ADMIN,
                "committee",
                {
                    "name": "bad",
                    "members": ["m1", "m2"],
                    "proxies": [dict(delegator="m1", proxy="ghost", **ACTIVE_PROXY)],
                },
            )

    def test_insufficient_seats_stays_pending_with_reason(self):
        _, application = self._setup_application(
            ["m1", "m2", "m3"], conflicts=["m2", "m3"]
        )
        app_id = application["id"]
        self._vote(app_id, "m1", "approve")
        last = self._vote(app_id, "m2", "abstain")
        self.assertEqual(last["status"], "under_review")
        self.assertIn("cannot reach 3 conflict-free approvals", last["data"]["pending_reason"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                ADMIN, app_id, "approve", {"terms": "t", "expires_at": "2099-01-01"}
            )


if __name__ == "__main__":
    unittest.main()
