import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [
            {'op': 'create', 'as': 'committee', 'kind': 'committee', 'data': {'name': 'DAC-1', 'members': ['alice', 'bob', 'carol', 'dave'], 'proxies': [{'delegator': 'dave', 'proxy': 'erin', 'starts_at': '2020-01-01T00:00:00+00:00', 'expires_at': '2099-01-01T00:00:00+00:00'}]}},
            {'op': 'create', 'as': 'dataset', 'kind': 'dataset', 'data': {'name': 'Rare Disease Cohort', 'access_policy': 'controlled'}},
            {'op': 'create', 'as': 'application', 'kind': 'application', 'data': {'dataset_id': '{dataset}', 'applicant_id': 'APP-1', 'purpose': 'variant analysis'}},
            {'op': 'transition', 'target': 'application', 'action': 'submit', 'data': {}, 'expect': 'submitted'},
            {'op': 'transition', 'target': 'application', 'action': 'review', 'data': {'committee_id': '{committee}', 'conflicts': ['carol']}, 'expect': 'under_review'},
            {'op': 'transition', 'target': 'application', 'action': 'vote', 'actor': ('alice', 'committee'), 'data': {'ballot': 'approve'}, 'expect': 'under_review'},
            {'op': 'transition', 'target': 'application', 'action': 'vote', 'actor': ('bob', 'committee'), 'data': {'ballot': 'approve'}, 'expect': 'under_review'},
            {'op': 'transition', 'target': 'application', 'action': 'vote', 'actor': ('erin', 'committee'), 'data': {'ballot': 'approve', 'on_behalf_of': 'dave'}, 'expect': 'under_review'},
            {'op': 'transition', 'target': 'application', 'action': 'vote', 'actor': ('carol', 'committee'), 'data': {'ballot': 'recuse'}, 'expect': 'under_review'},
            {'op': 'transition', 'target': 'application', 'action': 'approve', 'data': {'terms': 'noncommercial', 'expires_at': '2099-01-01'}, 'expect': 'approved'},
            {'op': 'create', 'as': 'grant', 'kind': 'grant', 'data': {'application_id': '{application}', 'dataset_id': '{dataset}', 'recipient': 'researcher-1'}},
            {'op': 'transition', 'target': 'grant', 'action': 'activate', 'data': {'starts_at': '2026-09-24', 'expires_at': '2099-01-01'}, 'expect': 'active'},
            {'op': 'transition', 'target': 'grant', 'action': 'revoke', 'data': {'reason': 'purpose changed'}, 'expect': 'revoked'},
        ]
        for step in steps:
            actor = Actor(*step["actor"]) if "actor" in step else self.actor
            if step["op"] == "create":
                entity = self.service.create(
                    actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])
        votes = entity and self.service.get(created["application"])["data"]["votes"]
        proxy_vote = [vote for vote in votes if vote["proxy"]][0]
        self.assertEqual(proxy_vote["member"], "dave")
        self.assertEqual(proxy_vote["voter"], "erin")


if __name__ == "__main__":
    unittest.main()
