from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("case requires an adverse sample")
    protection = sample["data"].get("protection") or {}
    if sample["status"] == "protected" or protection.get("protected"):
        raise ValidationError(
            "sample result is protected by a therapeutic use exemption"
        )
    if sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_exemption(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("exemption requires an active athlete")
    if not str(data.get("substance", "")).strip():
        raise ValidationError("substance is required")
    try:
        start = _date_ordinal(data.get("valid_from"))
        end = _date_ordinal(data.get("valid_to"))
    except (TypeError, ValueError):
        raise ValidationError("valid_from and valid_to must be ISO dates")
    if start > end:
        raise ValidationError("valid_from must not be after valid_to")
    supersedes = data.get("supersedes")
    if supersedes:
        previous = _find_one(lookup, "exemption", "id", supersedes)
        if not previous:
            raise ValidationError("superseded exemption not found: " + str(supersedes))
        if previous["data"].get("athlete_id") != data.get("athlete_id"):
            raise ValidationError("superseded exemption belongs to another athlete")


def _validate_independent_review(actor, entity, data, lookup):
    if actor.user_id == entity["created_by"]:
        raise PermissionDenied("reviewer must not be the applicant")
    return {"reviewed_by": actor.user_id}


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    determination = _determine_protection(entity, lookup)
    patch = {"confirmed_by": actor.user_id, "protection": determination}
    if determination["protected"]:
        return "protected", patch
    return None, patch


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'exemption': _validate_exemption}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('exemption', 'approve'): _validate_independent_review, ('exemption', 'reject'): _validate_independent_review}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'exemptions': 'exemption', 'tues': 'exemption'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'exemption': 'pending'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'exemption': {'approve': (('pending',), 'active'), 'reject': (('pending',), 'rejected'), 'void': (('pending', 'active'), 'voided')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule'), 'exemption': ('athlete_id', 'substance', 'valid_from', 'valid_to')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('exemption', 'reject'): ('reason',), ('exemption', 'void'): ('reason',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'exemption': ('doctor', 'admin')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'approve': ('reviewer', 'admin'), 'reject': ('reviewer', 'admin'), 'void': ('doctor', 'reviewer', 'admin')}

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
                override, extra = outcome
                next_status = override or next_status
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


def _sampled_on(sample):
    collected_at = sample["data"].get("collected_at")
    if not collected_at:
        return None
    return str(collected_at)[:10]


def _exemption_covers(exemption, sampled_on, substance):
    data = exemption["data"]
    try:
        start = _date_ordinal(data.get("valid_from"))
        end = _date_ordinal(data.get("valid_to"))
        day = _date_ordinal(sampled_on)
    except (TypeError, ValueError):
        return False
    if not start <= day <= end:
        return False
    covered = str(data.get("substance", "")).strip().lower()
    if substance and covered != str(substance).strip().lower():
        return False
    return True


def _determine_protection(sample, lookup):
    sampled_on = _sampled_on(sample)
    substance = sample["data"].get("substance")
    determination = {
        "protected": False,
        "sampled_on": sampled_on,
        "substance": substance,
        "exemption_id": None,
        "exemption_version": None,
        "determined_at": _utcnow(),
    }
    if not sampled_on:
        determination["reason"] = "sample has no collected_at date"
        return determination
    if lookup is None:
        return determination
    exemptions = lookup("exemption", "athlete_id", sample["data"].get("athlete_id")) or []
    for exemption in exemptions:
        if exemption["status"] != "active":
            continue
        if not _exemption_covers(exemption, sampled_on, substance):
            continue
        determination.update(
            {
                "protected": True,
                "exemption_id": exemption["id"],
                "exemption_version": exemption["version"],
            }
        )
        return determination
    return determination
