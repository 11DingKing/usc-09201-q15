"""调解卷分层、版本锁定与结案复核测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from service.domain import (
    ISSUE_BOUNDARY,
    ISSUE_ORAL_AGREEMENT,
    ISSUE_RENT,
    ORG_FORESTRY_STATION,
    ORG_JUDICIAL_OFFICE,
    PERFORMANCE_COMPLETE,
    PERFORMANCE_PARTIAL,
    SIGN_REFUSED,
    SIGN_SIGNED,
    Actor,
    ConflictError,
    MediationError,
    MediationService,
    NotFoundError,
    PermissionError,
    verify_hash_chain,
)
from service.privacy import (
    assert_public_payload_safe,
    build_public_progress,
    mask_id_number,
    mask_name,
)
from service.review import build_review

JUDICIAL = Actor("U1", "王调解", "mediator", ORG_JUDICIAL_OFFICE)
FORESTRY = Actor("U2", "李林政", "mediator", ORG_FORESTRY_STATION)
OUTSIDER = Actor("U9", "路人", "villager", None)

T0 = "2026-03-01T09:00:00+08:00"


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime.fromisoformat(T0)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, days: int = 0, hours: int = 0) -> None:
        from datetime import timedelta

        self.value += timedelta(days=days, hours=hours)


def build_boundary_case(service: MediationService) -> dict:
    """构造“边界争议受理→部分和解→未决移交诉讼”的完整卷宗。"""

    created = service.accept_case(
        JUDICIAL,
        title="青石沟林地边界与流转租金争议",
        category=ISSUE_BOUNDARY,
        summary="经营权流转加快后引发的边界、租金及口头约定争议",
        location={"village": "青石沟村", "compartment": "林班3", "place": "大弯丘", "geo": "27.x,106.y"},
    )
    case_id = created["case_id"]

    # 主体层：申请人（含未成年家庭成员，由监护人登记）、被申请人、第三人
    service.register_party(JUDICIAL, case_id, role="applicant", name="张大山",
                           id_number="520102198001011234", contact="13800001111")
    service.register_party(JUDICIAL, case_id, role="third_party", name="张小山",
                           minor=True, guardian_party_id="P1", privacy_tags=["minor", "family"])
    service.register_party(JUDICIAL, case_id, role="respondent", name="陈广财",
                           id_number="520102197505054321", contact="13900002222", is_org=False)

    # 争点层：边界、租金、历史口头约定三个争点，边界设举证期限
    service.open_issue(JUDICIAL, case_id, kind=ISSUE_BOUNDARY,
                       title="大弯丘林权边界四至争议",
                       evidence_deadline="2026-03-10T18:00:00+08:00")
    service.open_issue(FORESTRY, case_id, kind=ISSUE_RENT, title="2024-2025 年流转租金标准争议")
    service.open_issue(JUDICIAL, case_id, kind=ISSUE_ORAL_AGREEMENT,
                       title="2003 年口头换山约定是否成立", private=True)

    # 陈述与证据
    service.record_statement(FORESTRY, case_id, "P1",
                             "1982 年山林三定台账记载边界沿大弯丘水沟直上",
                             issue_id="I1", privacy_tags=["family"])
    service.record_statement(JUDICIAL, case_id, "P3",
                             "水沟在 1998 年修机耕道时已改道，应以老路为界", issue_id="I1")
    service.submit_evidence(FORESTRY, case_id, "P1", "I1",
                            "山林三定台账复印件一页，载有四至", kind="document",
                            received_at="2026-03-05T10:00:00+08:00")
    return {"case_id": case_id}


class DomainRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = MediationService(clock=self.clock)
        self.case_id = build_boundary_case(self.service)["case_id"]

    # -- 权限 ------------------------------------------------------------

    def test_only_mediator_from_joint_orgs_can_write(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.accept_case(OUTSIDER, title="x")
        # 林业站调解员可写（司法所与林业站共建）
        ok = self.service.open_issue(FORESTRY, self.case_id, kind=ISSUE_RENT, title="x")
        self.assertGreater(ok["case_version"], 0)

    # -- 证据迟到与版本锁定 ---------------------------------------------

    def test_late_evidence_is_marked_not_rejected(self) -> None:
        result = self.service.submit_evidence(
            JUDICIAL, self.case_id, "P3", "I1",
            "逾期补交的 GPS 打点记录", kind="survey",
            received_at="2026-03-12T09:00:00+08:00",
        )
        self.assertTrue(result["late"])
        view = self.service.load(self.case_id)
        late = [item for item in view.evidence if item.late]
        self.assertEqual(len(late), 1)
        self.assertNotIn("三定台账", late[0].summary)
        # 历史版本快照：v(迟到前) 看不到迟到证据
        before = self.service.load_at(self.case_id, result["case_version"] - 1)
        self.assertEqual(len(before.evidence), 1)

    def test_load_unknown_version_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.load_at(self.case_id, 999)

    # -- 代理人更换不覆盖旧陈述 -----------------------------------------

    def test_agent_replacement_keeps_old_statements_attributed(self) -> None:
        self.service.authorize_agent(JUDICIAL, self.case_id, "P1", name="周律师",
                                     scope="特别授权", agent_id="A1")
        self.service.record_statement(JUDICIAL, self.case_id, "A1",
                                      "代理意见：承认租金曾口头调整", issue_id="I2")
        self.service.authorize_agent(FORESTRY, self.case_id, "P1", name="吴法律工作者",
                                     scope="一般授权", agent_id="A2")
        view = self.service.load(self.case_id)
        self.assertFalse(view.agents["A1"].active)
        self.assertTrue(view.agents["A2"].active)
        # 旧陈述仍归属 A1（周律师），未被新代理人覆盖，且锁定在原版本序号
        old = next(s for s in view.statements if s.maker_id == "A1")
        self.assertEqual(old.version, 12)
        self.assertEqual(view.maker_name("A1"), "周律师（代理 张大山）")

    # -- 陈述补充只追加 --------------------------------------------------

    def test_supplementary_statement_appends_and_links(self) -> None:
        first = self.service.record_statement(JUDICIAL, self.case_id, "P3",
                                              "最初只记得口头约定在场四人", issue_id="I3")
        second = self.service.record_statement(
            JUDICIAL, self.case_id, "P3",
            "补充：实际在场五人，还有当时的生产队长",
            issue_id="I3", supersedes=first["statement_id"], materials=["情况说明.pdf"])
        view = self.service.load(self.case_id)
        statements = [s for s in view.statements if s.issue_id == "I3"]
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0].content, "最初只记得口头约定在场四人")
        self.assertEqual(second["statement_id"], statements[1].statement_id)
        self.assertEqual(statements[1].supersedes, statements[0].statement_id)

    # -- 并行调解：争点不得相交 -----------------------------------------

    def test_parallel_sessions_cannot_share_issue(self) -> None:
        self.service.start_session(JUDICIAL, self.case_id, issue_ids=["I1"])
        # 另一条并行线可调解不相交的租金争点
        self.service.start_session(FORESTRY, self.case_id, issue_ids=["I2"])
        with self.assertRaises(ConflictError):
            self.service.start_session(JUDICIAL, self.case_id, issue_ids=["I1", "I3"])
        # 会议结束后争点释放
        self.service.conclude_session(JUDICIAL, self.case_id, "M1", note="边界双方各让一步")
        self.service.start_session(FORESTRY, self.case_id, issue_ids=["I1", "I3"])

    # -- 争点单独达成/撤回共识 ------------------------------------------

    def test_per_issue_consensus_reach_and_withdraw(self) -> None:
        self.service.propose_plan(
            JUDICIAL, self.case_id,
            terms={"I1": "以现水沟为界，双方各退一米设置标记带"},
            basis_evidence_ids=["E1"], note="边界一揽子方案",
        )
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=1, consenters=["P1", "P3"])
        view = self.service.load(self.case_id)
        self.assertEqual(len(view.resolved_issues()), 1)
        self.assertEqual(view.unresolved_issues()[0].issue_id, "I2")
        with self.assertRaises(ConflictError):
            self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                         version=1, consenters=["P1"])
        self.service.withdraw_consensus(JUDICIAL, self.case_id, "I1",
                                        by_party_id="P3", reason="标记带宽度未谈拢")
        view = self.service.load(self.case_id)
        self.assertIsNone(view.issues["I1"].active_consensus())
        # 历史共识记录仍保留
        self.assertEqual(view.issues["I1"].consensus[0].withdraw_reason, "标记带宽度未谈拢")

    def test_consensus_must_reference_real_plan_version(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.reach_consensus(JUDICIAL, self.case_id, "I1", version=9,
                                         consenters=["P1"])

    # -- 签收与部分履行 --------------------------------------------------

    def test_signoff_partial_performance_and_withdraw_block(self) -> None:
        self.service.propose_plan(
            JUDICIAL, self.case_id,
            terms={"I1": "以现水沟为界"},
            basis_evidence_ids=["E1"],
        )
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=1, consenters=["P1", "P3"])
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", 1, "P1", status=SIGN_SIGNED)
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", 1, "P3", status=SIGN_REFUSED)
        self.service.record_performance(JUDICIAL, self.case_id, "I1",
                                        PERFORMANCE_PARTIAL, progress=50,
                                        note="东段已埋桩，西段待清理")
        self.service.withdraw_consensus(JUDICIAL, self.case_id, "I1", reason="反悔")
        # 继续履行至完毕后不得再撤回
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=1, consenters=["P1", "P3"])
        self.service.record_performance(JUDICIAL, self.case_id, "I1",
                                        PERFORMANCE_COMPLETE, progress=100, note="界桩全部完成")
        with self.assertRaises(ConflictError):
            self.service.withdraw_consensus(JUDICIAL, self.case_id, "I1")

    # -- 移交诉讼锁定与结案 ---------------------------------------------

    def test_transfer_locks_case_and_closes_with_partial_settlement(self) -> None:
        self.service.propose_plan(
            JUDICIAL, self.case_id, terms={"I1": "以现水沟为界"},
            basis_evidence_ids=["E1"],
        )
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=1, consenters=["P1", "P3"])
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", 1, "P1")
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", 1, "P3")
        self.service.record_performance(JUDICIAL, self.case_id, "I1",
                                        PERFORMANCE_COMPLETE, progress=100)
        transfer = self.service.transfer_to_litigation(
            JUDICIAL, self.case_id, reason="租金与口头约定分歧过大",
            handover_note="随卷移交台账、笔录及逾期 GPS 材料")
        locked_version = transfer["locked_version"]
        self.assertEqual(transfer["unresolved_issues"], ["I2", "I3"])
        # 移交后卷宗锁定
        with self.assertRaises(ConflictError):
            self.service.record_statement(JUDICIAL, self.case_id, "P1", "事后补充", issue_id="I2")
        with self.assertRaises(ConflictError):
            self.service.authorize_agent(JUDICIAL, self.case_id, "P1", name="新律师")
        # 锁前版本可回放
        locked = self.service.load_at(self.case_id, locked_version)
        self.assertEqual(locked.status, "litigation_transferred")
        self.assertEqual(locked.transfer["handover_evidence_ids"], [])
        self.service.close_case(JUDICIAL, self.case_id, note="边界部分和解，其余进入诉讼")
        view = self.service.load(self.case_id)
        self.assertEqual(view.status, "closed")

    def test_cannot_close_with_unresolved_issue_before_transfer(self) -> None:
        with self.assertRaises(ConflictError):
            self.service.close_case(JUDICIAL, self.case_id)


class FullLifecycleReviewTest(unittest.TestCase):
    """结案复核：回放一次边界争议从受理到部分和解的全过程。"""

    def setUp(self) -> None:
        self.service = MediationService()
        self.case_id = build_boundary_case(self.service)["case_id"]

    def _settle_boundary(self) -> None:
        self.service.start_session(JUDICIAL, self.case_id, issue_ids=["I1"],
                                   participant_ids=["P1", "P3"])
        # 被申请人在会议中补交一份关键证据
        late = self.service.submit_evidence(
            JUDICIAL, self.case_id, "P3", "I1",
            "1998 年机耕道施工说明，证明水沟改道",
            received_at="2026-03-11T09:00:00+08:00")
        self.assertTrue(late["late"])
        plan = self.service.propose_plan(
            JUDICIAL, self.case_id,
            terms={"I1": "以老路与现水沟中线为界，双方共同设置界桩"},
            basis_evidence_ids=["E1", "E2"],
        )
        self.plan_version = plan["version"]
        self.basis_version = plan["basis_version"]
        self.service.conclude_session(JUDICIAL, self.case_id, "M1",
                                      note="边界部分达成一致，租金另议")
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=self.plan_version, consenters=["P1", "P3"])
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", self.plan_version, "P1")
        self.service.sign_plan(JUDICIAL, self.case_id, "PL", self.plan_version, "P3")
        self.service.record_performance(JUDICIAL, self.case_id, "I1",
                                        PERFORMANCE_PARTIAL, progress=60,
                                        note="界桩完成 60%")
        # 未决争点的陈述随卷移交诉讼
        self.service.record_statement(
            JUDICIAL, self.case_id, "P1",
            "2024 年起对方按每年每亩 80 元支付，后单方降到 50 元", issue_id="I2")
        self.service.record_statement(
            JUDICIAL, self.case_id, "P3",
            "2003 年换山只是临时看管，不存在永久互换", issue_id="I3")

    def test_review_reconstructs_who_agreed_what_when_and_handover(self) -> None:
        self._settle_boundary()
        transfer = self.service.transfer_to_litigation(
            JUDICIAL, self.case_id, reason="租金、口头约定未决",
            handover_note="移交租金凭证及口头约定笔录")
        close = self.service.close_case(JUDICIAL, self.case_id, note="部分和解后结案")

        report = build_review(self.service, self.case_id)

        # 完整性：哈希链未被破坏
        self.assertTrue(report["integrity_ok"])
        self.assertEqual(report["replayed_version"], close["case_version"])
        self.assertEqual(report["status"], "closed")

        # 谁在何时同意了什么：边界共识锁定到方案 v1（依据卷宗版本与证据）
        boundary = next(a for a in report["agreements"] if a["issue_id"] == "I1")
        self.assertEqual(boundary["state"], "active")
        self.assertEqual(boundary["plan_version"], self.plan_version)
        self.assertEqual(boundary["consenters"], ["张大山", "陈广财"])
        self.assertTrue(boundary["agreed_at"])
        self.assertNotIn("张小山", boundary["consenters"])  # 未成年人未列入同意人

        # 签收状态可回放
        signoffs = {(s["party"], s["status"]): s for s in report["signoffs"]}
        self.assertIn(("张大山", "signed"), signoffs)
        self.assertIn(("陈广财", "signed"), signoffs)

        # 部分履行进度
        perf = next(p for p in report["performance"] if p["issue_id"] == "I1")
        self.assertEqual(perf["status_label"], "部分履行")
        self.assertEqual(perf["progress"], 60)

        # 未解决部分如何移交：锁定版本、随卷证据与陈述清单
        self.assertEqual(report["resolved_issues"], ["I1"])
        unresolved_ids = {item["issue_id"] for item in report["unresolved_issues"]}
        self.assertEqual(unresolved_ids, {"I2", "I3"})
        transfer_info = report["litigation_transfer"]
        self.assertEqual(transfer_info["locked_version"], transfer["locked_version"])
        self.assertGreaterEqual(len(transfer_info["handover_statement_ids"]), 2)
        self.assertIn("unresolved_issues", transfer_info)

        # 时间线覆盖受理到结案全部关键事件
        types = [entry["type"] for entry in report["timeline"]]
        for expected in [
            "case_accepted", "party_registered", "issue_opened",
            "evidence_submitted", "plan_proposed", "consensus_reached",
            "performance_recorded", "litigation_transferred", "case_closed",
        ]:
            self.assertIn(expected, types)
        # 迟到证据在时间线中被显式标注
        late_entries = [
            e for e in report["timeline"]
            if e["type"] == "evidence_submitted" and e["detail"]["late"]
        ]
        self.assertEqual(len(late_entries), 1)

    def test_review_can_replay_historical_locked_version(self) -> None:
        self._settle_boundary()
        transfer = self.service.transfer_to_litigation(JUDICIAL, self.case_id)
        self.service.close_case(JUDICIAL, self.case_id)
        # 结案后回放移交诉讼时锁定的历史版本
        at_lock = build_review(self.service, self.case_id, version=transfer["locked_version"])
        self.assertTrue(at_lock["locked_review"])
        self.assertEqual(at_lock["status"], "litigation_transferred")
        self.assertIsNone(at_lock["closed_at"])
        self.assertNotIn("case_closed", [e["type"] for e in at_lock["timeline"]])
        # 与最新卷宗对照：最新版已结案
        latest = build_review(self.service, self.case_id)
        self.assertEqual(latest["status"], "closed")

    def test_hash_chain_detects_tampering(self) -> None:
        self._settle_boundary()
        events = self.service._cases[self.case_id]  # noqa: SLF001
        self.assertTrue(verify_hash_chain(events))
        # 事后篡改某条证据摘要（模拟绕过应用层改库）
        target = next(e for e in events if e.type == "evidence_submitted")
        target.data["summary"] = "被篡改的内容"
        self.assertFalse(verify_hash_chain(events))


class PublicPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MediationService()
        self.case_id = build_boundary_case(self.service)["case_id"]

    def test_public_progress_is_masked_and_safe(self) -> None:
        view = self.service.load(self.case_id)
        payload = build_public_progress(view, "YLD-TEST1234")
        assert_public_payload_safe(payload)
        # 不泄露内部卷宗号、身份信息、小地名与坐标
        text = repr(payload)
        for secret in ["张大山", "陈广财", "张小山", "520102", "1380000",
                       "大弯丘", "青石沟", "minor", "family", "27.x", "台账"]:
            self.assertNotIn(secret, text)
        # 只返回争点类型与进度阶段
        kinds = {item["kind"] for item in payload["issues"]}
        self.assertEqual(kinds, {ISSUE_BOUNDARY, ISSUE_RENT, ISSUE_ORAL_AGREEMENT})
        self.assertEqual(payload["status_label"], "已受理")

    def test_public_progress_tracks_partial_and_transfer(self) -> None:
        self.service.propose_plan(JUDICIAL, self.case_id,
                                  terms={"I1": "以现水沟为界"},
                                  basis_evidence_ids=["E1"])
        self.service.reach_consensus(JUDICIAL, self.case_id, "I1",
                                     version=1, consenters=["P1", "P3"])
        self.service.record_performance(JUDICIAL, self.case_id, "I1",
                                        PERFORMANCE_PARTIAL, progress=40)
        self.service.transfer_to_litigation(JUDICIAL, self.case_id)
        payload = build_public_progress(self.service.load(self.case_id), "YLD-X")
        assert_public_payload_safe(payload)
        self.assertEqual(payload["resolved_count"], 1)
        self.assertEqual(payload["unresolved_count"], 2)
        self.assertTrue(payload["litigation_transferred"])
        self.assertEqual(payload["status_label"], "未决部分已移交诉讼")
        boundary = next(i for i in payload["issues"] if i["kind"] == ISSUE_BOUNDARY)
        self.assertEqual(boundary["stage"], "部分履行")
        self.assertEqual(boundary["progress_percent"], 40)

    def test_mask_helpers(self) -> None:
        self.assertEqual(mask_name("张大山"), "张*山")
        self.assertEqual(mask_name("张山"), "张*")
        self.assertEqual(mask_id_number("520102198001011234"), "520" + "*" * 12 + "234")


if __name__ == "__main__":
    unittest.main()
