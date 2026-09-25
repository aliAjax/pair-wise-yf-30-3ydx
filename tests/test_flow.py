import sys
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


class PharmacovigilanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1"):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def create_at(self, dedupe, received_at):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": received_at, "serious": False},
        )["case"]

    def review(self, case_id, expected, serious=True, fatal=False):
        return self.svc.medical_review(
            case_id, "reviewer-1", "medical_reviewer",
            {"expected_revision": expected, "serious": serious, "fatal": fatal,
             "causality": "possibly_related", "rationale": "资料已核验", "received_at": iso(utcnow())},
        )

    def test_followup_recalculates_unsubmitted_report(self):
        # 非严重案例 100 天前收到，报告时限（90 天）已过
        case = self.create_at("sync-1", iso(utcnow() - timedelta(days=100)))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.escalate_overdue("lead-cn", "regional_lead", "CN")
        stale = self.svc.get_case(case["id"], "global_admin", "")["reports"][0]
        self.assertEqual(stale["status"], "overdue")
        self.assertEqual([r["id"] for r in self.svc.overdue("regional_lead", "CN")], [report["id"]])
        # 随访把接收时间更新为现在，同案未提交报告时限一并重算
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "补充实验室检查", "source": "phone", "expected_revision": 1,
                               "received_at": iso(utcnow())})
        detail = self.svc.get_case(case["id"], "global_admin", "")
        synced = detail["reports"][0]
        self.assertEqual(synced["due_at"], detail["case"]["report_due_at"])
        self.assertEqual(synced["status"], "pending")
        self.assertEqual(self.svc.overdue("regional_lead", "CN"), [])

    def test_medical_review_recalculates_unsubmitted_report(self):
        case = self.create_at("sync-2", iso(utcnow()))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        old_due = report["due_at"]
        self.review(case["id"], 1, serious=True, fatal=True)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        synced = detail["reports"][0]
        self.assertEqual(synced["status"], "pending")
        self.assertEqual(synced["due_at"], detail["case"]["report_due_at"])
        self.assertNotEqual(synced["due_at"], old_due)

    def test_submitted_report_becomes_re_report_after_review(self):
        case = self.create_at("sync-3", iso(utcnow() - timedelta(days=100)))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})["report"]
        # 提交后医学审核调高严重性：报告转待重报，保留原提交人和时间
        self.review(case["id"], 1, serious=True)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        flagged = detail["reports"][0]
        self.assertEqual(flagged["status"], "re_report")
        self.assertEqual(flagged["submitted_by"], "lead-cn")
        self.assertEqual(flagged["submitted_at"], submitted["submitted_at"])
        self.assertEqual(flagged["due_at"], detail["case"]["report_due_at"])
        # 触发待重报的这次裁定本身就是严重性重审，可直接重报
        resubmitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})["report"]
        self.assertEqual(resubmitted["status"], "submitted")

    def test_re_report_blocked_until_severity_reviewed(self):
        case = self.create_at("sync-4", iso(utcnow()))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        # 随访触发待重报，但随访后严重性未重审，先挡住
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "新的随访信息", "source": "email", "expected_revision": 1})
        self.assertEqual(self.svc.get_case(case["id"], "global_admin", "")["reports"][0]["status"], "re_report")
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "severity_review_required")
        # 重审后可以重报
        self.review(case["id"], 2, serious=True)
        resubmitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})["report"]
        self.assertEqual(resubmitted["status"], "submitted")

    def test_new_followup_after_review_blocks_re_report_again(self):
        case = self.create_at("sync-5", iso(utcnow()))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第一次随访", "source": "email", "expected_revision": 1})
        self.review(case["id"], 2, serious=True)
        # 重审之后又来了新随访，需再次重审才能重报
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第二次随访", "source": "fax", "expected_revision": 3})
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "severity_review_required")

    def test_re_report_region_restriction(self):
        case = self.create_at("sync-6", iso(utcnow()))
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "随访信息", "source": "email", "expected_revision": 1})
        self.review(case["id"], 2, serious=True)
        # 其他区域负责人不能重报本区域报告
        with self.assertRaises(ApiError) as ctx:
            self.svc.submit_report(report["id"], "lead-us", "regional_lead", "US", {})
        self.assertEqual(ctx.exception.status, 403)
        resubmitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})["report"]
        self.assertEqual(resubmitted["status"], "submitted")

    def test_full_case_and_deduplication_flow(self):
        case = self.create()
        self.assertEqual(case["revision"], 1)
        followed = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "住院并出现死亡转归", "source": "phone", "expected_revision": 1,
             "received_at": iso(utcnow())},
        )
        self.assertEqual(followed["revision"], 2)
        reviewed = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 2, "serious": True, "fatal": True, "causality": "possibly_related",
             "rationale": "住院记录和死亡证明已核验", "received_at": iso(utcnow())},
        )
        self.assertEqual(reviewed["case"]["revision"], 3)
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        submitted = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        duplicate = self.svc.create_case(
            "reporter-b", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "fax", "dedupe_key": "intake-1", "received_at": iso(utcnow())},
        )
        self.assertTrue(duplicate["deduplicated"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 1)
        self.assertGreaterEqual(len(detail["audit"]), 5)
        self.assertEqual(submitted["report"]["late"], 0)

    def test_permissions_and_stale_revision(self):
        case = self.create("intake-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_case(case["id"], "reporter", "US")
        self.assertEqual(ctx.exception.status, 403)
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "第一次更新", "source": "email", "expected_revision": 1})
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "过期修改", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(case["id"], "lead-cn", "regional_lead",
                                    {"expected_revision": 2, "serious": True, "fatal": False,
                                     "causality": "related", "rationale": "x", "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
