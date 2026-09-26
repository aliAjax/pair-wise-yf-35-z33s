import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.doctor = Actor("doc-1", "doctor")
        self.other_doctor = Actor("doc-2", "doctor")
        self.reviewer = Actor("panel-1", "panel")
        self.lab = Actor("lab-1", "lab")
        self.admin = Actor("admin", "admin")
        self.athlete_id = self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _analyzed_sample(self, sample_code, collected_at):
        sample = self.service.create(
            self.admin,
            "sample",
            {
                "athlete_id": self.athlete_id,
                "sample_code": sample_code,
                "event": "national-final",
            },
        )
        sample_id = sample["id"]
        for action, data in [
            ("collect", {"collected_at": collected_at}),
            ("seal", {"seal_id": "SEAL-" + sample_code}),
            ("ship", {"carrier": "Courier-A"}),
            ("receive", {"lab_id": "LAB-1"}),
        ]:
            sample = self.service.transition(self.admin, sample_id, action, data)
        return sample_id

    def _tue(self, substances, valid_from, valid_to, actor=None, **extra):
        data = {
            "athlete_id": self.athlete_id,
            "substances": substances,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
        data.update(extra)
        return self.service.create(actor or self.doctor, "tue", data)

    def _adverse_sample(self, sample_code, collected_at, substances):
        sample_id = self._analyzed_sample(sample_code, collected_at)
        self.service.transition(
            self.lab, sample_id, "analyze", {"result": "adverse", "substances": substances}
        )
        return sample_id

    def test_tue_lifecycle_requires_independent_reviewer(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-06-30")
        self.assertEqual(tue["status"], "pending")
        self.assertEqual(tue["created_by"], "doc-1")
        # The applicant cannot approve their own application.
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.doctor, tue["id"], "approve", {})
        # A lab technician cannot review a TUE.
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.lab, tue["id"], "approve", {})
        approved = self.service.transition(self.reviewer, tue["id"], "approve", {})
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["data"]["reviewed_by"], "panel-1")
        self.assertTrue(approved["data"]["reviewed_at"])

    def test_viewer_cannot_register_tue(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("viewer-1", "viewer"),
                "tue",
                {
                    "athlete_id": self.athlete_id,
                    "substances": ["x"],
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-01-02",
                },
            )

    def test_tue_validates_dates_and_substances(self):
        with self.assertRaises(ValidationError):
            self._tue([], "2026-01-01", "2026-01-02")
        with self.assertRaises(ValidationError):
            self._tue(["x"], "2026-02-01", "2026-01-01")
        with self.assertRaises(ValidationError):
            self._tue(["x"], "not-a-date", "2026-01-02")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.doctor,
                "tue",
                {
                    "athlete_id": "missing-athlete",
                    "substances": ["x"],
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-01-02",
                },
            )

    def test_valid_tue_on_sample_day_marks_result_protected(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-03-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-001", "2026-02-15T08:00:00Z", ["terbutaline"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "protected")
        determination = reported["data"]["tue_determination"]
        self.assertTrue(determination["protected"])
        self.assertEqual(determination["sample_day"], "2026-02-15")
        self.assertEqual(determination["detected_substances"], ["terbutaline"])
        self.assertEqual(
            [item["tue_id"] for item in determination["covering_tues"]], [tue["id"]]
        )
        # A protected result cannot open a case.
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "case",
                {
                    "athlete_id": self.athlete_id,
                    "sample_id": sample_id,
                    "alleged_rule": "substance-1",
                },
            )

    def test_boundary_days_are_covered(self):
        tue = self._tue(["s"], "2026-02-15", "2026-02-15")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample("S-002", "2026-02-15", ["s"])
        self.assertEqual(
            self.service.transition(self.lab, sample_id, "report_adverse", {})["status"],
            "protected",
        )

    def test_tue_outside_sample_window_is_adverse(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-01-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-003", "2026-02-15T08:00:00Z", ["terbutaline"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "adverse")
        self.assertFalse(reported["data"]["tue_determination"]["protected"])
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": self.athlete_id,
                "sample_id": sample_id,
                "alleged_rule": "substance-1",
            },
        )
        self.assertEqual(case["status"], "open")

    def test_pending_or_rejected_tue_does_not_protect(self):
        pending = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        sample_id = self._adverse_sample(
            "S-004", "2026-02-15T08:00:00Z", ["terbutaline"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "adverse")
        # Approving afterwards cannot rewrite the frozen determination.
        approved = self.service.transition(self.reviewer, pending["id"], "approve", {})
        self.assertEqual(approved["status"], "active")
        again = self.service.get(sample_id)
        self.assertEqual(again["status"], "adverse")
        self.assertFalse(again["data"]["tue_determination"]["protected"])

    def test_uncovered_substance_is_adverse(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-005", "2026-02-15T08:00:00Z", ["EPO"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "adverse")

    def test_all_detected_substances_must_be_covered(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-006", "2026-02-15T08:00:00Z", ["terbutaline", "EPO"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "adverse")

    def test_multiple_tues_can_jointly_cover(self):
        first = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        second = self._tue(["EPO"], "2026-01-01", "2026-12-31")
        self.service.transition(self.reviewer, first["id"], "approve", {})
        self.service.transition(self.reviewer, second["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-007", "2026-02-15T08:00:00Z", ["terbutaline", "EPO"]
        )
        reported = self.service.transition(self.lab, sample_id, "report_adverse", {})
        self.assertEqual(reported["status"], "protected")
        self.assertEqual(
            len(reported["data"]["tue_determination"]["covering_tues"]), 2
        )

    def test_later_revocation_does_not_rewrite_protected_determination(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        sample_id = self._adverse_sample(
            "S-008", "2026-02-15T08:00:00Z", ["terbutaline"]
        )
        self.service.transition(self.lab, sample_id, "report_adverse", {})
        # Revoke the TUE after the lab reported; the sample stays protected.
        revoked = self.service.transition(
            self.reviewer, tue["id"], "revoke", {"reason": "new evidence"}
        )
        self.assertEqual(revoked["status"], "revoked")
        sample = self.service.get(sample_id)
        self.assertEqual(sample["status"], "protected")
        self.assertTrue(sample["data"]["tue_determination"]["protected"])
        # The snapshot even retains the TUE version captured at reporting time.
        snapshot = sample["data"]["tue_determination"]["covering_tues"][0]
        self.assertEqual(snapshot["status"], "active")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "case",
                {
                    "athlete_id": self.athlete_id,
                    "sample_id": sample_id,
                    "alleged_rule": "substance-1",
                },
            )

    def test_revoke_requires_reason_and_active_tue(self):
        tue = self._tue(["s"], "2026-01-01", "2026-01-02")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer, tue["id"], "revoke", {"reason": "x"}
            )
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        with self.assertRaises(ValidationError):
            self.service.transition(self.reviewer, tue["id"], "revoke", {})
        revoked = self.service.transition(
            self.doctor, tue["id"], "revoke", {"reason": "contraindication found"}
        )
        self.assertEqual(revoked["data"]["revoked_by"], "doc-1")

    def test_reject_requires_reason_and_is_terminal(self):
        tue = self._tue(["s"], "2026-01-01", "2026-01-02")
        with self.assertRaises(ValidationError):
            self.service.transition(self.reviewer, tue["id"], "reject", {})
        rejected = self.service.transition(
            self.reviewer, tue["id"], "reject", {"reason": "insufficient documentation"}
        )
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.reviewer, tue["id"], "approve", {})

    def test_reapplication_keeps_full_history(self):
        original = self._tue(["terbutaline"], "2026-01-01", "2026-03-31")
        self.service.transition(self.reviewer, original["id"], "approve", {})
        self.service.transition(
            self.reviewer, original["id"], "revoke", {"reason": "treatment changed"}
        )
        renewal = self._tue(
            ["terbutaline"],
            "2026-04-01",
            "2026-06-30",
            actor=self.other_doctor,
            replaces_tue_id=original["id"],
        )
        self.assertEqual(renewal["data"]["replaces_tue_id"], original["id"])
        # Both records remain, and the athlete's TUE history is queryable.
        history = self.service.list("tues", athlete_id=self.athlete_id)
        self.assertEqual({item["id"] for item in history}, {original["id"], renewal["id"]})
        self.assertEqual(self.service.get(original["id"])["status"], "revoked")
        # A replacement cannot reference another athlete's TUE.
        other_athlete = self.service.create(
            self.admin, "athlete", {"name": "B. Runner", "discipline": "athletics"}
        )["id"]
        with self.assertRaises(ValidationError):
            self.service.create(
                self.doctor,
                "tue",
                {
                    "athlete_id": other_athlete,
                    "substances": ["x"],
                    "valid_from": "2026-04-01",
                    "valid_to": "2026-06-30",
                    "replaces_tue_id": original["id"],
                },
            )

    def test_results_view_flags_protection_and_cases(self):
        tue = self._tue(["terbutaline"], "2026-01-01", "2026-12-31")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        protected_id = self._adverse_sample(
            "S-100", "2026-02-15T08:00:00Z", ["terbutaline"]
        )
        self.service.transition(self.lab, protected_id, "report_adverse", {})
        adverse_id = self._adverse_sample("S-101", "2026-03-15T08:00:00Z", ["EPO"])
        self.service.transition(self.lab, adverse_id, "report_adverse", {})
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": self.athlete_id,
                "sample_id": adverse_id,
                "alleged_rule": "substance-1",
            },
        )
        results = {item["sample_id"]: item for item in self.service.results()}
        protected_item = results[protected_id]
        adverse_item = results[adverse_id]
        self.assertTrue(protected_item["protected"])
        self.assertEqual(protected_item["status"], "protected")
        self.assertEqual(protected_item["case_ids"], [])
        self.assertFalse(adverse_item["protected"])
        self.assertEqual(adverse_item["case_ids"], [case["id"]])
        # Filtering by athlete works on the view as well.
        self.assertEqual(
            {item["sample_id"] for item in self.service.results(athlete_id=self.athlete_id)},
            {protected_id, adverse_id},
        )
        other_athlete = self.service.create(
            self.admin, "athlete", {"name": "B. Runner", "discipline": "athletics"}
        )["id"]
        self.assertEqual(self.service.results(athlete_id=other_athlete), [])

    def test_audit_trail_records_independent_review(self):
        tue = self._tue(["s"], "2026-01-01", "2026-01-02")
        self.service.transition(self.reviewer, tue["id"], "approve", {})
        actions = [
            entry["action"]
            for entry in self.service.audit_log(tue["id"])
        ]
        actors = {
            entry["action"]: entry["actor_id"]
            for entry in self.service.audit_log(tue["id"])
        }
        self.assertEqual(actions, ["create", "approve"])
        self.assertEqual(actors["create"], "doc-1")
        self.assertEqual(actors["approve"], "panel-1")


if __name__ == "__main__":
    unittest.main()
