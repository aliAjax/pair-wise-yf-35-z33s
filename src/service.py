from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, athlete_id=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        if athlete_id:
            items = [item for item in items if item["data"].get("athlete_id") == athlete_id]
        return items

    def results(self, athlete_id=None):
        """Results-management view over analyzed samples.

        Each item surfaces the frozen TUE determination (protected or not)
        and the case opened from the sample, so protected results are
        distinguishable from actionable adverse findings.
        """
        samples = [
            sample
            for sample in self.list("sample")
            if sample["status"] in ("analyzed", "adverse", "protected", "cleared")
        ]
        cases = self.list("case")
        cases_by_sample = {}
        for case in cases:
            cases_by_sample.setdefault(case["data"].get("sample_id"), []).append(case["id"])
        items = []
        for sample in samples:
            if athlete_id and sample["data"].get("athlete_id") != athlete_id:
                continue
            determination = sample["data"].get("tue_determination") or {}
            items.append(
                {
                    "sample_id": sample["id"],
                    "sample_code": sample["data"].get("sample_code"),
                    "athlete_id": sample["data"].get("athlete_id"),
                    "result": sample["data"].get("result"),
                    "status": sample["status"],
                    "protected": bool(determination.get("protected")),
                    "detected_substances": determination.get("detected_substances")
                    or sample["data"].get("substances", []),
                    "sample_day": determination.get("sample_day")
                    or str(sample["data"].get("collected_at", ""))[:10],
                    "covering_tues": determination.get("covering_tues", []),
                    "determined_at": determination.get("determined_at"),
                    "case_ids": cases_by_sample.get(sample["id"], []),
                }
            )
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
