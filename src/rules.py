from datetime import date, datetime, timezone

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _day(value):
    """Accept YYYY-MM-DD or a full ISO timestamp and return its calendar day."""
    text = str(value).strip()
    if not text:
        raise ValidationError("date is required")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValidationError("invalid date: %s" % value)


def _normalize_substances(data):
    """Substances may be supplied as ``substances`` or ``substance``."""
    values = data.get("substances")
    if values is None:
        single = data.get("substance")
        values = [single] if single not in (None, "") else []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValidationError("substances must be a list")
    substances = []
    for item in values:
        name = str(item or "").strip()
        if name and name not in substances:
            substances.append(name)
    return substances


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
    if not sample or sample["status"] not in ("adverse", "protected"):
        raise ValidationError("case requires an adverse sample")
    determination = sample["data"].get("tue_determination") or {}
    if sample["status"] == "protected" or determination.get("protected"):
        raise ValidationError("sample is protected by a valid TUE; cannot open a case")


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


def _validate_tue(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("tue requires an active athlete")
    substances = _normalize_substances(data)
    if not substances:
        raise ValidationError("at least one substance is required")
    data["substances"] = substances
    data.pop("substance", None)
    valid_from = _day(data.get("valid_from"))
    valid_to = _day(data.get("valid_to"))
    if valid_to < valid_from:
        raise ValidationError("valid_to must be on or after valid_from")
    replaces = data.get("replaces_tue_id")
    if replaces:
        previous = _find_one(lookup, "tue", "id", replaces)
        if not previous:
            raise ValidationError("replaces_tue_id does not reference a TUE")
        if previous["data"].get("athlete_id") != athlete["id"]:
            raise ValidationError("a replacement TUE must belong to the same athlete")


def _validate_tue_approve(actor, entity, data, lookup):
    if actor.user_id == entity["created_by"]:
        raise PermissionDenied("the reviewer must differ from the applicant")
    return {"reviewed_by": actor.user_id, "reviewed_at": _utcnow()}


def _validate_tue_reject(actor, entity, data, lookup):
    if actor.user_id == entity["created_by"]:
        raise PermissionDenied("the reviewer must differ from the applicant")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise ValidationError("reason is required")
    return {"reviewed_by": actor.user_id, "reviewed_at": _utcnow(), "reject_reason": reason}


def _validate_tue_revoke(actor, entity, data, lookup):
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise ValidationError("reason is required")
    return {"revoked_by": actor.user_id, "revoked_at": _utcnow(), "revoke_reason": reason}


def _validate_sample_analyze(actor, entity, data, lookup):
    substances = _normalize_substances(data)
    if substances:
        data["substances"] = substances
    return {}


def _tue_snapshot(tue):
    return {
        "tue_id": tue["id"],
        "substances": list(tue["data"].get("substances", [])),
        "valid_from": tue["data"].get("valid_from"),
        "valid_to": tue["data"].get("valid_to"),
        "status": tue["status"],
        "approved_at": tue["data"].get("reviewed_at"),
        "version": tue["version"],
    }


def _tue_active_on(tue, sample_day):
    """A TUE is effective on a day when it is approved and the day falls
    inside its declared coverage window."""
    if tue["status"] != "active":
        return False
    try:
        valid_from = _day(tue["data"].get("valid_from"))
        valid_to = _day(tue["data"].get("valid_to"))
    except ValidationError:
        return False
    return valid_from <= sample_day <= valid_to


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    sample_day = _day(entity["data"].get("collected_at"))
    detected = _normalize_substances(entity["data"])
    if not detected:
        detected = _normalize_substances(data)
    candidates = lookup("tue", "athlete_id", entity["data"].get("athlete_id")) if lookup else []
    # Collect TUEs effective on the sampling day; several TUEs may jointly
    # cover the set of detected substances.
    covering = []
    covered_substances = set()
    for tue in candidates or []:
        if not _tue_active_on(tue, sample_day):
            continue
        substances = set(tue["data"].get("substances", []))
        if not (substances & set(detected)):
            continue
        covered_substances |= substances & set(detected)
        snapshot = _tue_snapshot(tue)
        if snapshot not in covering:
            covering.append(snapshot)
    protected = set(detected).issubset(covered_substances) if detected else False
    # The determination is frozen on the sample at reporting time: later
    # TUE approval, revocation or reapplication cannot rewrite it.
    determination = {
        "protected": protected,
        "sample_day": sample_day.isoformat(),
        "detected_substances": detected,
        "covering_tues": covering if protected else [],
        "determined_by": actor.user_id,
        "determined_at": _utcnow(),
    }
    return {"confirmed_by": actor.user_id, "tue_determination": determination}


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'case': _validate_case,
    'tue': _validate_tue,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('sample', 'analyze'): _validate_sample_analyze,
    ('case', 'decide'): _validate_case_decision,
    ('case', 'resolve_appeal'): _validate_case_decision,
    ('tue', 'approve'): _validate_tue_approve,
    ('tue', 'reject'): _validate_tue_reject,
    ('tue', 'revoke'): _validate_tue_revoke,
}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'tues': 'tue'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'tue': 'pending'}
    TRANSITIONS = {
        'athlete': {'retire': (('active',), 'retired')},
        'sample': {
            'collect': (('scheduled',), 'collected'),
            'seal': (('collected',), 'sealed'),
            'ship': (('sealed',), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'analyze': (('received',), 'analyzed'),
            'report_adverse': (('analyzed',), 'adverse'),
            'clear': (('analyzed',), 'cleared'),
        },
        'case': {
            'provisional_suspend': (('open',), 'suspended'),
            'schedule_hearing': (('suspended',), 'hearing'),
            'decide': (('hearing',), 'closed'),
            'appeal': (('closed',), 'appeal'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
        'tue': {
            'approve': (('pending',), 'active'),
            'reject': (('pending',), 'rejected'),
            'revoke': (('active',), 'revoked'),
        },
    }
    # Actions whose resulting status depends on the domain decision returned
    # by their custom validator (e.g. a TUE-protected adverse finding).
    CONDITIONAL_STATUS = {('sample', 'report_adverse'): {True: 'protected', False: 'adverse'}}
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule'),
        'tue': ('athlete_id', 'substances', 'valid_from', 'valid_to'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('tue', 'reject'): ('reason',),
        ('tue', 'revoke'): ('reason',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'case': ('admin', 'panel'),
        'tue': ('admin', 'doctor'),
    }
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'provisional_suspend': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        ('tue', 'approve'): ('admin', 'panel'),
        ('tue', 'reject'): ('admin', 'panel'),
        ('tue', 'revoke'): ('admin', 'panel', 'doctor'),
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
        conditional = self.CONDITIONAL_STATUS.get((kind, action))
        if conditional:
            protected = bool(extra.get("tue_determination", {}).get("protected"))
            next_status = conditional[protected]
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
