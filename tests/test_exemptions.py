import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ExemptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.doctor = Actor("dr-1", "doctor")
        self.reviewer = Actor("rev-1", "reviewer")
        self.admin = Actor("admin-1", "admin")
        self.lab = Actor("lab-1", "lab")
        self.athlete = self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _exemption(self, **overrides):
        data = {
            "athlete_id": self.athlete["id"],
            "substance": "prednisone",
            "valid_from": "2026-01-01",
            "valid_to": "2026-01-31",
        }
        data.update(overrides)
        return self.service.create(self.doctor, "exemption", data)

    def _analyzed_sample(self, collected_at="2026-01-10T09:00:00Z", substance="prednisone"):
        sample = self.service.create(
            self.admin,
            "sample",
            {
                "athlete_id": self.athlete["id"],
                "sample_code": "S-100",
                "event": "national-final",
            },
        )
        self.service.transition(
            self.admin, sample["id"], "collect", {"collected_at": collected_at}
        )
        self.service.transition(self.admin, sample["id"], "seal", {"seal_id": "SEAL-1"})
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier-A"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        analyze = {"result": "adverse"}
        if substance is not None:
            analyze["substance"] = substance
        self.service.transition(self.lab, sample["id"], "analyze", analyze)
        return sample

    def _report(self, sample_id):
        return self.service.transition(self.lab, sample_id, "report_adverse", {})

    def test_exemption_needs_independent_approval(self):
        exemption = self._exemption()
        self.assertEqual(exemption["status"], "pending")
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.doctor, exemption["id"], "approve", {})
        approved = self.service.transition(self.reviewer, exemption["id"], "approve", {})
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["data"]["reviewed_by"], "rev-1")

    def test_exemption_registration_roles_and_dates(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("viewer-1", "viewer"),
                "exemption",
                {
                    "athlete_id": self.athlete["id"],
                    "substance": "prednisone",
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-01-31",
                },
            )
        with self.assertRaises(ValidationError):
            self._exemption(valid_from="2026-02-01", valid_to="2026-01-01")
        with self.assertRaises(ValidationError):
            self._exemption(valid_from="not-a-date")
        with self.assertRaises(ValidationError):
            self._exemption(substance="  ")

    def test_valid_exemption_protects_result_and_blocks_case(self):
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        sample = self._analyzed_sample()
        reported = self._report(sample["id"])
        self.assertEqual(reported["status"], "protected")
        protection = reported["data"]["protection"]
        self.assertTrue(protection["protected"])
        self.assertEqual(protection["exemption_id"], exemption["id"])
        self.assertEqual(protection["sampled_on"], "2026-01-10")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "case",
                {
                    "athlete_id": self.athlete["id"],
                    "sample_id": sample["id"],
                    "alleged_rule": "substance-1",
                },
            )

    def test_no_coverage_or_substance_mismatch_stays_adverse(self):
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        outside = self._analyzed_sample(collected_at="2026-02-10T09:00:00Z")
        self.assertEqual(self._report(outside["id"])["status"], "adverse")
        mismatch = self._analyzed_sample(substance="epo")
        reported = self._report(mismatch["id"])
        self.assertEqual(reported["status"], "adverse")
        self.assertFalse(reported["data"]["protection"]["protected"])
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": self.athlete["id"],
                "sample_id": mismatch["id"],
                "alleged_rule": "substance-1",
            },
        )
        self.assertEqual(case["status"], "open")

    def test_pending_exemption_does_not_protect(self):
        self._exemption()
        sample = self._analyzed_sample()
        self.assertEqual(self._report(sample["id"])["status"], "adverse")

    def test_later_exemption_changes_do_not_rewrite_determination(self):
        sample = self._analyzed_sample()
        reported = self._report(sample["id"])
        self.assertEqual(reported["status"], "adverse")
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        after = self.service.get(sample["id"])
        self.assertEqual(after["status"], "adverse")
        self.assertFalse(after["data"]["protection"]["protected"])

        protected_sample = self._analyzed_sample()
        protected_report = self._report(protected_sample["id"])
        self.assertEqual(protected_report["status"], "protected")
        self.service.transition(
            self.doctor, exemption["id"], "void", {"reason": "reapplied"}
        )
        still = self.service.get(protected_sample["id"])
        self.assertEqual(still["status"], "protected")
        self.assertTrue(still["data"]["protection"]["protected"])
        self.assertEqual(still["data"]["protection"]["exemption_id"], exemption["id"])

    def test_void_and_reapply_keep_history(self):
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        voided = self.service.transition(
            self.doctor, exemption["id"], "void", {"reason": "treatment changed"}
        )
        self.assertEqual(voided["status"], "voided")
        reapplied = self._exemption(
            valid_from="2026-02-01", valid_to="2026-02-28", supersedes=exemption["id"]
        )
        self.assertEqual(reapplied["status"], "pending")
        self.assertEqual(reapplied["data"]["supersedes"], exemption["id"])
        self.service.transition(self.reviewer, reapplied["id"], "approve", {})
        stored = self.service.get(exemption["id"])
        self.assertEqual(stored["status"], "voided")
        exemptions = self.service.list("exemption")
        self.assertEqual(len(exemptions), 2)
        actions = [
            entry["action"] for entry in self.service.audit_log(entity_id=exemption["id"])
        ]
        self.assertEqual(actions, ["create", "approve", "void"])

    def test_supersedes_must_match_athlete(self):
        other_athlete = self.service.create(
            self.admin, "athlete", {"name": "B. Sprinter", "discipline": "sprint"}
        )
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        self.service.transition(
            self.doctor, exemption["id"], "void", {"reason": "reapplied"}
        )
        with self.assertRaises(ValidationError):
            self._exemption(athlete_id=other_athlete["id"], supersedes=exemption["id"])
        with self.assertRaises(ValidationError):
            self._exemption(supersedes="missing-id")

    def test_result_overview_marks_protected_results(self):
        exemption = self._exemption()
        self.service.transition(self.reviewer, exemption["id"], "approve", {})
        protected_sample = self._analyzed_sample()
        self._report(protected_sample["id"])
        adverse_sample = self._analyzed_sample(collected_at="2026-03-01T09:00:00Z")
        self._report(adverse_sample["id"])
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": self.athlete["id"],
                "sample_id": adverse_sample["id"],
                "alleged_rule": "substance-1",
            },
        )
        overview = self.service.result_overview()["items"]
        self.assertEqual(len(overview), 2)
        by_sample = {item["sample_id"]: item for item in overview}
        protected_item = by_sample[protected_sample["id"]]
        self.assertTrue(protected_item["protected"])
        self.assertEqual(protected_item["exemption_id"], exemption["id"])
        self.assertIsNone(protected_item["case_id"])
        adverse_item = by_sample[adverse_sample["id"]]
        self.assertFalse(adverse_item["protected"])
        self.assertEqual(adverse_item["case_id"], case["id"])


if __name__ == "__main__":
    unittest.main()
