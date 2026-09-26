from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

REQUIRED_APPROVALS = 3
BALLOTS = ("approve", "reject", "recuse")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_instant(value, field):
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(field + " must be an ISO date or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")


def _validate_members(members):
    if not isinstance(members, list) or not members:
        raise ValidationError("members must be a non-empty list")
    seen = set()
    for member in members:
        if not isinstance(member, str) or not member.strip():
            raise ValidationError("committee members must be non-empty names")
        if member in seen:
            raise ValidationError("duplicate committee member: " + member)
        seen.add(member)
    return seen


def _validate_proxies(proxies, members):
    delegators = set()
    for entry in proxies:
        if not isinstance(entry, dict):
            raise ValidationError("proxy entries must be objects")
        delegator = entry.get("delegator")
        proxy = entry.get("proxy")
        if delegator not in members:
            raise ValidationError("proxy delegator is not a committee member: %s" % delegator)
        if not isinstance(proxy, str) or not proxy.strip():
            raise ValidationError("proxy voter is required for: %s" % delegator)
        if proxy == delegator:
            raise ValidationError("member cannot be their own proxy: " + delegator)
        if delegator in delegators:
            raise ValidationError("duplicate proxy registration for: " + delegator)
        delegators.add(delegator)
        starts = _parse_instant(entry.get("starts_at"), "proxy starts_at")
        expires = _parse_instant(entry.get("expires_at"), "proxy expires_at")
        if expires <= starts:
            raise ValidationError("proxy expires_at must be after starts_at")


def _validate_committee(actor, data, lookup):
    members = _validate_members(data.get("members"))
    _validate_proxies(data.get("proxies") or [], members)


def _validate_committee_update(actor, entity, data, lookup):
    merged = dict(entity["data"])
    merged.update(data)
    members = _validate_members(merged.get("members"))
    _validate_proxies(merged.get("proxies") or [], members)


def _validate_review(actor, entity, data, lookup):
    conflicts = data.pop("conflicts", None) or []
    committee = _find_one(lookup, "committee", "id", data.get("committee_id"))
    if not committee:
        raise ValidationError("committee does not exist")
    if committee["status"] != "active":
        raise ValidationError("committee is not active")
    members = list(committee["data"].get("members") or [])
    panel_conflicts = []
    for member in conflicts:
        if member not in members:
            raise ValidationError("conflicted member is not on the committee: %s" % member)
        if member not in panel_conflicts:
            panel_conflicts.append(member)
    # 进入审阅即锁定有资格委员、冲突名单和代理登记，之后的委员会调整不影响本次审阅
    panel = {
        "members": members,
        "conflicts": panel_conflicts,
        "proxies": list(committee["data"].get("proxies") or []),
        "locked_at": _now(),
    }
    return {"panel": panel, "votes": [], "pending_reason": ""}


def _active_proxy(panel, delegator, voter):
    now = datetime.now(timezone.utc)
    for entry in panel.get("proxies") or []:
        if entry.get("delegator") == delegator and entry.get("proxy") == voter:
            starts = _parse_instant(entry.get("starts_at"), "proxy starts_at")
            expires = _parse_instant(entry.get("expires_at"), "proxy expires_at")
            if starts <= now <= expires:
                return True
    return False


def _validate_vote(actor, entity, data, lookup):
    ballot = data.pop("ballot")
    on_behalf_of = data.pop("on_behalf_of", None)
    if ballot not in BALLOTS:
        raise ValidationError("ballot must be one of: " + ", ".join(BALLOTS))
    panel = entity["data"].get("panel") or {}
    members = panel.get("members") or []
    if not members:
        raise InvalidTransition("review panel is not locked yet")
    seat = on_behalf_of or actor.user_id
    if seat not in members:
        raise PermissionDenied("voter is not on the locked panel: " + str(seat))
    is_proxy = seat != actor.user_id
    if is_proxy and not _active_proxy(panel, seat, actor.user_id):
        raise PermissionDenied(
            "no valid proxy registration for %s on behalf of %s" % (actor.user_id, seat)
        )
    votes = list(entity["data"].get("votes") or [])
    if any(vote.get("member") == seat for vote in votes):
        raise ConflictError("panel member has already voted: " + seat)
    if seat in (panel.get("conflicts") or []) and ballot != "recuse":
        raise PermissionDenied("conflicted member must recuse: " + seat)
    # 代理票同时记录委托人（member）与投票人（voter）
    record = {
        "member": seat,
        "ballot": ballot,
        "voter": actor.user_id,
        "proxy": is_proxy,
        "cast_at": _now(),
    }
    return {"votes": votes + [record]}


def _validate_approve(actor, entity, data, lookup):
    panel = entity["data"].get("panel") or {}
    conflicts = set(panel.get("conflicts") or [])
    votes = entity["data"].get("votes") or []
    approvals = [
        vote
        for vote in votes
        if vote.get("ballot") == "approve" and vote.get("member") not in conflicts
    ]
    rejections = [vote for vote in votes if vote.get("ballot") == "reject"]
    if len(approvals) >= REQUIRED_APPROVALS and not rejections:
        if not data.get("terms") or not data.get("expires_at"):
            raise ValidationError("terms and expires_at are required to approve")
        decision = {
            "outcome": "approved",
            "approvals": len(approvals),
            "rejections": len(rejections),
            "recusals": sum(1 for vote in votes if vote.get("ballot") == "recuse"),
            "decided_at": _now(),
        }
        return "approved", {"decision": decision, "pending_reason": ""}
    voted = {vote.get("member") for vote in votes}
    outstanding = [m for m in panel.get("members") or [] if m not in voted]
    # 票数不足：申请留在待审，说明缺票原因
    reasons = []
    if len(approvals) < REQUIRED_APPROVALS:
        reasons.append(
            "conflict-free approvals %d/%d" % (len(approvals), REQUIRED_APPROVALS)
        )
    if outstanding:
        reasons.append("awaiting ballots from: " + ", ".join(outstanding))
    if rejections:
        reasons.append(
            "opposition ballots from: " + ", ".join(vote.get("member") for vote in rejections)
        )
    return "under_review", {"pending_reason": "; ".join(reasons)}


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_grant_activate(actor, entity, data, lookup):
    if data.get("expires_at") < data.get("starts_at"):
        raise ValidationError("grant expiry must be after start")
    return {"activated_by": actor.user_id}


CUSTOM_CREATE = {'dataset': _validate_dataset, 'application': _validate_application, 'committee': _validate_committee}
CUSTOM_TRANSITIONS = {('application', 'review'): _validate_review, ('application', 'vote'): _validate_vote, ('application', 'approve'): _validate_approve, ('grant', 'activate'): _validate_grant_activate, ('committee', 'update'): _validate_committee_update}


class RuleEngine:
    ALIASES = {'datasets': 'dataset', 'applications': 'application', 'grants': 'grant', 'committees': 'committee'}
    INITIAL_STATUS = {'dataset': 'registered', 'application': 'draft', 'grant': 'issued', 'committee': 'active'}
    TRANSITIONS = {'dataset': {'restrict': (('registered',), 'restricted'), 'publish': (('restricted',), 'published')}, 'application': {'submit': (('draft',), 'submitted'), 'review': (('submitted',), 'under_review'), 'vote': (('under_review',), 'under_review'), 'approve': (('under_review',), 'approved'), 'reject': (('under_review',), 'rejected'), 'withdraw': (('submitted', 'under_review'), 'withdrawn')}, 'grant': {'activate': (('issued',), 'active'), 'revoke': (('active',), 'revoked'), 'expire': (('active',), 'expired')}, 'committee': {'update': (('active',), 'active')}}
    CREATE_REQUIRED = {'dataset': ('name', 'access_policy'), 'application': ('dataset_id', 'applicant_id', 'purpose'), 'grant': ('application_id', 'dataset_id', 'recipient'), 'committee': ('members',)}
    ACTION_REQUIRED = {('dataset', 'restrict'): ('reason',), ('application', 'review'): ('committee_id',), ('application', 'vote'): ('ballot',), ('application', 'reject'): ('reason',), ('application', 'withdraw'): ('reason',), ('grant', 'activate'): ('starts_at', 'expires_at'), ('grant', 'revoke'): ('reason',), ('grant', 'expire'): ('expired_at',)}
    CREATE_ROLES = {'dataset': ('admin', 'committee'), 'application': ('admin', 'applicant'), 'grant': ('admin', 'committee'), 'committee': ('admin',)}
    ROLE_ACTIONS = {'restrict': ('admin', 'committee'), 'publish': ('admin', 'committee'), 'submit': ('admin', 'applicant'), 'review': ('admin', 'committee'), 'approve': ('admin', 'committee'), 'reject': ('admin', 'committee'), 'withdraw': ('admin', 'applicant'), 'activate': ('admin', 'committee'), 'revoke': ('admin', 'committee'), 'expire': ('admin', 'committee'), ('application', 'vote'): ('committee',), ('committee', 'update'): ('admin',)}

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
        extra = {}
        if custom:
            outcome = custom(actor, entity, data, lookup)
            if isinstance(outcome, tuple):
                # 自定义校验可覆盖目标状态（如计票不足时 approve 留在 under_review）
                next_status, extra = outcome
            elif outcome:
                extra = outcome
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
