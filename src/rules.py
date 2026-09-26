from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

REQUIRED_APPROVALS = 3
VOTE_CHOICES = ("approve", "reject", "abstain")


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")


def _validate_roster(members, conflicts, proxies):
    if (
        not isinstance(members, list)
        or not members
        or any(not isinstance(m, str) or not m for m in members)
        or len(set(members)) != len(members)
    ):
        raise ValidationError("members must be a non-empty list of distinct member ids")
    member_set = set(members)
    unknown_conflicts = [m for m in conflicts if m not in member_set]
    if unknown_conflicts:
        raise ValidationError(
            "conflicted members are not in the roster: " + ", ".join(unknown_conflicts)
        )
    for entry in proxies:
        delegator = entry.get("delegator")
        proxy = entry.get("proxy")
        starts_at = entry.get("starts_at")
        expires_at = entry.get("expires_at")
        if not all([delegator, proxy, starts_at, expires_at]):
            raise ValidationError(
                "proxy entries require delegator, proxy, starts_at and expires_at"
            )
        if delegator == proxy:
            raise ValidationError("proxy delegator and proxy must differ")
        if delegator not in member_set or proxy not in member_set:
            raise ValidationError("proxy delegator and proxy must be committee members")
        if str(expires_at)[:10] < str(starts_at)[:10]:
            raise ValidationError("proxy expiry must not be before its start")


def _validate_committee(actor, data, lookup):
    _validate_roster(
        data.get("members"),
        data.get("conflicts") or [],
        data.get("proxies") or [],
    )


def _validate_amend(actor, entity, data, lookup):
    current = entity.get("data", {})
    if not any(key in data for key in ("members", "conflicts", "proxies")):
        raise ValidationError("nothing to amend")
    _validate_roster(
        data.get("members", current.get("members")),
        data.get("conflicts", current.get("conflicts") or []),
        data.get("proxies", current.get("proxies") or []),
    )


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _period_active(entry, as_of):
    starts = str(entry.get("starts_at", ""))[:10]
    expires = str(entry.get("expires_at", ""))[:10]
    return starts <= as_of <= expires


def _summarize_votes(votes, eligible, conflicts):
    conflicted = set(conflicts)
    approvals = sum(
        1 for v in votes if v["vote"] == "approve" and v["member"] not in conflicted
    )
    rejections = sum(1 for v in votes if v["vote"] == "reject")
    abstentions = sum(1 for v in votes if v["vote"] == "abstain")
    voted = {v["member"] for v in votes}
    outstanding = [m for m in eligible if m not in voted]
    return {
        "approvals": approvals,
        "rejections": rejections,
        "abstentions": abstentions,
        "outstanding": outstanding,
        "quorum_met": approvals >= REQUIRED_APPROVALS and rejections == 0,
    }


def _pending_reason(tally, conflicts):
    if tally["quorum_met"]:
        return None
    parts = []
    if tally["rejections"]:
        parts.append("blocked by %d rejection vote(s)" % tally["rejections"])
    missing = REQUIRED_APPROVALS - tally["approvals"]
    if missing > 0:
        parts.append("missing %d conflict-free approval(s)" % missing)
    if tally["outstanding"]:
        parts.append("unvoted seats: %s" % ", ".join(tally["outstanding"]))
    conflicted = set(conflicts)
    open_seats = [m for m in tally["outstanding"] if m not in conflicted]
    max_possible = tally["approvals"] + len(open_seats)
    if max_possible < REQUIRED_APPROVALS:
        parts.append(
            "cannot reach %d conflict-free approvals (max possible: %d)"
            % (REQUIRED_APPROVALS, max_possible)
        )
    return "; ".join(parts)


def _vote_snapshot(votes, eligible, conflicts):
    tally = _summarize_votes(votes, eligible, conflicts)
    return {
        "votes": votes,
        "tally": tally,
        "pending_reason": _pending_reason(tally, conflicts),
    }


def _validate_review(actor, entity, data, lookup):
    committee = _find_one(lookup, "committee", "id", data.get("committee_id"))
    if not committee:
        raise ValidationError("committee does not exist")
    roster = committee["data"]
    eligible = list(roster.get("members") or [])
    conflicts = list(roster.get("conflicts") or [])
    snapshot = _vote_snapshot([], eligible, conflicts)
    snapshot.update(
        {
            "eligible_members": eligible,
            "conflicts": conflicts,
            "proxies": list(roster.get("proxies") or []),
        }
    )
    return snapshot


def _validate_vote(actor, entity, data, lookup):
    vote = data.pop("vote", None)
    on_behalf_of = data.pop("on_behalf_of", None)
    if vote not in VOTE_CHOICES:
        raise ValidationError("vote must be one of: " + ", ".join(VOTE_CHOICES))
    info = entity.get("data", {})
    eligible = info.get("eligible_members") or []
    conflicts = info.get("conflicts") or []
    proxies = info.get("proxies") or []
    votes = info.get("votes") or []
    today = _today()
    if on_behalf_of:
        if on_behalf_of not in eligible:
            raise PermissionDenied("delegator is not an eligible member of this review")
        delegation = next(
            (
                entry
                for entry in proxies
                if entry.get("delegator") == on_behalf_of
                and entry.get("proxy") == actor.user_id
                and _period_active(entry, today)
            ),
            None,
        )
        if not delegation:
            raise PermissionDenied("no active proxy delegation for this delegator")
        seat = on_behalf_of
    else:
        if actor.user_id not in eligible:
            raise PermissionDenied("not an eligible member of this review")
        delegated = any(
            entry.get("delegator") == actor.user_id and _period_active(entry, today)
            for entry in proxies
        )
        if delegated:
            raise PermissionDenied(
                "member has an active proxy delegation; the proxy must cast this vote"
            )
        seat = actor.user_id
    if any(v["member"] == seat for v in votes):
        raise ConflictError("this seat has already voted")
    conflicted = set(conflicts)
    if (seat in conflicted or actor.user_id in conflicted) and vote != "abstain":
        raise PermissionDenied("conflicted member can only abstain")
    record = {
        "member": seat,
        "voter": actor.user_id,
        "vote": vote,
        "proxy": bool(on_behalf_of),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _vote_snapshot(votes + [record], eligible, conflicts)


def _validate_approve(actor, entity, data, lookup):
    info = entity.get("data", {})
    tally = _summarize_votes(
        info.get("votes") or [],
        info.get("eligible_members") or [],
        info.get("conflicts") or [],
    )
    if not tally["quorum_met"]:
        reason = _pending_reason(tally, info.get("conflicts") or [])
        raise ValidationError("cannot approve: " + (reason or "committee quorum not met"))


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_grant_activate(actor, entity, data, lookup):
    if data.get("expires_at") < data.get("starts_at"):
        raise ValidationError("grant expiry must be after start")
    return {"activated_by": actor.user_id}


CUSTOM_CREATE = {
    'dataset': _validate_dataset,
    'application': _validate_application,
    'committee': _validate_committee,
}
CUSTOM_TRANSITIONS = {
    ('application', 'review'): _validate_review,
    ('application', 'vote'): _validate_vote,
    ('application', 'approve'): _validate_approve,
    ('grant', 'activate'): _validate_grant_activate,
    ('committee', 'amend'): _validate_amend,
}


class RuleEngine:
    ALIASES = {
        'datasets': 'dataset',
        'applications': 'application',
        'grants': 'grant',
        'committees': 'committee',
    }
    INITIAL_STATUS = {
        'dataset': 'registered',
        'application': 'draft',
        'grant': 'issued',
        'committee': 'registered',
    }
    TRANSITIONS = {
        'dataset': {
            'restrict': (('registered',), 'restricted'),
            'publish': (('restricted',), 'published'),
        },
        'application': {
            'submit': (('draft',), 'submitted'),
            'review': (('submitted',), 'under_review'),
            'vote': (('under_review',), 'under_review'),
            'approve': (('under_review',), 'approved'),
            'reject': (('under_review',), 'rejected'),
            'withdraw': (('submitted', 'under_review'), 'withdrawn'),
        },
        'grant': {
            'activate': (('issued',), 'active'),
            'revoke': (('active',), 'revoked'),
            'expire': (('active',), 'expired'),
        },
        'committee': {
            'amend': (('registered',), 'registered'),
        },
    }
    CREATE_REQUIRED = {
        'dataset': ('name', 'access_policy'),
        'application': ('dataset_id', 'applicant_id', 'purpose'),
        'grant': ('application_id', 'dataset_id', 'recipient'),
        'committee': ('name', 'members'),
    }
    ACTION_REQUIRED = {
        ('dataset', 'restrict'): ('reason',),
        ('application', 'review'): ('committee_id',),
        ('application', 'vote'): ('vote',),
        ('application', 'approve'): ('terms', 'expires_at'),
        ('application', 'reject'): ('reason',),
        ('application', 'withdraw'): ('reason',),
        ('grant', 'activate'): ('starts_at', 'expires_at'),
        ('grant', 'revoke'): ('reason',),
        ('grant', 'expire'): ('expired_at',),
    }
    CREATE_ROLES = {
        'dataset': ('admin', 'committee'),
        'application': ('admin', 'applicant'),
        'grant': ('admin', 'committee'),
        'committee': ('admin',),
    }
    ROLE_ACTIONS = {
        'restrict': ('admin', 'committee'),
        'publish': ('admin', 'committee'),
        'submit': ('admin', 'applicant'),
        'review': ('admin', 'committee'),
        'vote': ('committee',),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'withdraw': ('admin', 'applicant'),
        'activate': ('admin', 'committee'),
        'revoke': ('admin', 'committee'),
        'expire': ('admin', 'committee'),
        'amend': ('admin',),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
