"""林地争议调解卷领域模型测试。

以“一卷边界 + 租金争议”从受理到部分和解、租金争点转入诉讼的
全过程为主线，并覆盖迟到证据、授权变更、并行调解与脱敏查询。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from service.dossier import (
    Dossier,
    DossierError,
    IssueType,
    ParticipantRole,
    PERFORMANCE_DONE,
    PERFORMANCE_PARTIAL,
    PERFORMANCE_PENDING,
)

BASE = datetime(2026, 3, 1, 9, 0)


def t(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return BASE + timedelta(days=day, hours=hour - 9, minutes=minute)


class DossierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dossier = Dossier(
            "LD-2026-001", t(0), evidence_deadline=t(10)
        )
        self.dossier.add_participant(
            "p1", "林大山", ParticipantRole.APPLICANT, t(0, 10)
        )
        self.dossier.add_participant(
            "p2", "合作社", ParticipantRole.RESPONDENT, t(0, 11), kind="单位"
        )
        self.dossier.open_issue(
            "i1", IssueType.BOUNDARY, "东界沟至杉木林一线边界", t(1)
        )
        self.dossier.open_issue(
            "i2", IssueType.RENT, "2024、2025 年流转租金差额", t(1)
        )

    # ----- 版本不覆盖：补充材料与更换代理人 ---------------------------

    def test_statements_are_versioned_and_never_overwritten(self) -> None:
        self.dossier.record_statement("s1", "p1", "边界以老石埂为准", t(2))
        self.dossier.record_statement("s2", "p1", "经重新回忆，边界应为新挖界沟", t(5))
        statements = self.dossier.statements("p1")
        self.assertEqual([item["id"] for item in statements], ["s1", "s2"])
        self.assertEqual(statements[0]["content"], "边界以老石埂为准")
        self.assertEqual(statements[1]["content"], "经重新回忆，边界应为新挖界沟")

    def test_agent_change_keeps_old_statement_attribution(self) -> None:
        self.dossier.change_agent("p1", "周律师", t(2), basis="书面委托")
        self.dossier.record_statement(
            "s1", "p1", "代理人代述：租金每年八百元/亩", t(3), via_agent=True
        )
        self.dossier.change_agent("p1", "吴法律工作者", t(6), basis="重新委托")
        self.dossier.record_statement("s2", "p1", "本人补充陈述", t(7))

        statements = self.dossier.statements("p1")
        self.assertEqual(statements[0]["agent_name"], "周律师")
        self.assertIsNone(statements[1]["agent_name"])

        authorizations = self.dossier.subjects()[0]["authorizations"]
        self.assertEqual(len(authorizations), 2)
        self.assertEqual(authorizations[0]["agent_name"], "周律师")
        self.assertEqual(authorizations[0]["ended_at"], t(6))
        self.assertIsNone(authorizations[1]["ended_at"])

    def test_statement_via_agent_requires_valid_authorization_at_that_time(self) -> None:
        self.dossier.change_agent("p1", "周律师", t(4))
        self.dossier.revoke_agent("p1", t(8))
        with self.assertRaisesRegex(DossierError, "没有生效中的代理授权"):
            self.dossier.record_statement(
                "s9", "p1", "撤销后代述无效", t(9), via_agent=True
            )

    # ----- 迟到证据与共识证据快照 -------------------------------------

    def test_late_evidence_is_flagged_but_accepted(self) -> None:
        self.assertFalse(
            self.dossier.submit_evidence(
                "e1", "林权证", "四至：东界沟", "p1", t(3), issue_id="i1"
            )
        )
        self.assertTrue(
            self.dossier.submit_evidence(
                "e2", "租地口头约定录音", "村会计在场复述", "p2", t(12), issue_id="i2"
            )
        )
        evidence = self.dossier.evidence_summaries()
        self.assertEqual([item["late"] for item in evidence], [False, True])

    def test_consensus_snapshots_evidence_seqs_and_late_evidence_does_not_rewrite_it(
        self,
    ) -> None:
        self.dossier.submit_evidence("e1", "林权证", "四至", "p1", t(3), issue_id="i1")
        self.dossier.open_session("m1", "王调解员", t(4), venue="乡调解室")
        version = self.dossier.reach_consensus(
            "i1", "东界以界沟为准，双方共同栽桩", t(4, 14), "m1"
        )
        self.assertEqual(version, 1)
        consensus = self.dossier.issues()[0]["consensus"][0]
        early_seq = consensus["basis_evidence_seqs"]

        # 举证期限过后才到的证据进入卷宗，但不改变 v1 快照。
        self.dossier.submit_evidence("e2", "老照片", "1998 年边界照片", "p2", t(12), issue_id="i1")
        consensus_after = self.dossier.issues()[0]["consensus"][0]
        self.assertEqual(consensus_after["basis_evidence_seqs"], early_seq)
        self.assertNotIn(self.dossier.evidence_summaries()[-1]["seq"], early_seq)

    # ----- 逐争点共识、撤回与重新达成 ---------------------------------

    def test_consensus_can_be_reached_and_withdrawn_per_issue(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.assertEqual(self.dossier.issues()[0]["active_version"], 1)
        self.assertEqual(self.dossier.issues()[1]["active_version"], None)

        withdrawn = self.dossier.withdraw_consensus(
            "i1", t(6), by="林大山", reason="新发现老照片显示界沟被改道"
        )
        self.assertEqual(withdrawn, 1)
        issue = self.dossier.issues()[0]
        self.assertIsNone(issue["active_version"])
        self.assertIsNotNone(issue["consensus"][0]["withdrawn"])

        # 撤回后可基于新证据达成新版本，历史版本保留。
        self.dossier.reach_consensus("i1", "边界以老石埂为准", t(13), "m1")
        issue = self.dossier.issues()[0]
        self.assertEqual(issue["active_version"], 2)
        self.assertEqual(len(issue["consensus"]), 2)
        self.assertEqual(issue["consensus"][0]["version"], 1)

    def test_consensus_requires_both_parties(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        with self.assertRaisesRegex(DossierError, "争议双方全体"):
            self.dossier.reach_consensus(
                "i1", "单方同意无效", t(4, 10), "m1", agreed_by=["p1"]
            )

    def test_consensus_must_happen_within_an_open_session(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.close_session("m1", t(4, 12))
        with self.assertRaisesRegex(DossierError, "不在会话"):
            self.dossier.reach_consensus("i1", "闭会后的共识无效", t(4, 15), "m1")

    def test_parallel_sessions_are_supported(self) -> None:
        # 两场会话时间重叠：边界在乡调解室，租金在林业站并行进行。
        self.dossier.open_session("m1", "王调解员", t(4, 9), venue="乡调解室")
        self.dossier.open_session("m2", "陈林业工程师", t(4, 9), venue="林业站")
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.reach_consensus("i2", "租金按每亩七百元结算", t(4, 11), "m2")
        self.dossier.close_session("m1", t(4, 12))
        self.dossier.close_session("m2", t(4, 12, 30))
        issues = self.dossier.issues()
        self.assertEqual(issues[0]["consensus"][0]["session_id"], "m1")
        self.assertEqual(issues[1]["consensus"][0]["session_id"], "m2")

    # ----- 调解书封存、签收不可覆盖与部分履行 -------------------------

    def test_sealed_plan_locks_consensus_versions(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界 v1", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))

        plan = self.dossier.plans()[0]
        self.assertEqual(plan["issue_versions"], {"i1": 1})

        # 封存锁定版本：不能撤回被引用的共识，也不能再达成新共识。
        with self.assertRaisesRegex(DossierError, "已写入封存的调解书"):
            self.dossier.withdraw_consensus("i1", t(6), by="林大山", reason="反悔")
        with self.assertRaisesRegex(DossierError, "已写入封存的调解书"):
            self.dossier.reach_consensus("i1", "边界 v2", t(7), "m1")

    def test_signoff_cannot_be_overwritten(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))
        self.dossier.sign_plan("plan-1", "p1", t(6))
        with self.assertRaisesRegex(DossierError, "签收状态不可覆盖"):
            self.dossier.sign_plan("plan-1", "p1", t(7))

    def test_partial_performance_history_is_retained(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准并栽桩", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))
        self.dossier.sign_plan("plan-1", "p1", t(6))
        self.dossier.sign_plan("plan-1", "p2", t(6))
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_PENDING, t(10), by="合作社", note="尚未动工"
        )
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_PARTIAL, t(15), by="王调解员", note="已栽东段三棵桩"
        )
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_DONE, t(20), by="王调解员", note="全线栽桩完成"
        )
        history = self.dossier.plans()[0]["performance"]["i1"]
        self.assertEqual(
            [item["status"] for item in history],
            [PERFORMANCE_PENDING, PERFORMANCE_PARTIAL, PERFORMANCE_DONE],
        )
        self.assertEqual(self.dossier.overall_status(), "部分和解")

    # ----- 转入诉讼：锁定版本、冻结卷宗、移交未解决争点 ---------------

    def test_transfer_locks_version_and_freezes_dossier(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))
        self.dossier.sign_plan("plan-1", "p1", t(6))
        self.dossier.sign_plan("plan-1", "p2", t(6))
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_DONE, t(9), by="王调解员"
        )
        version_before = self.dossier.version
        transfer = self.dossier.transfer_to_litigation(
            t(20), by="陈林业工程师", handover_note="租金差额协商破裂，移交法庭"
        )
        self.assertEqual(transfer["issue_ids"], ("i2",))
        self.assertEqual(transfer["locked_seq"], version_before)
        self.assertEqual(self.dossier.overall_status(), "已移交诉讼")

        # 锁定后任何写入都被拒绝。
        with self.assertRaisesRegex(DossierError, "转入诉讼并锁定"):
            self.dossier.open_issue(
                "i3", IssueType.VERBAL, "锁定后新增争点无效", t(21)
            )
        with self.assertRaisesRegex(DossierError, "转入诉讼并锁定"):
            self.dossier.submit_evidence("e9", "新材料", "摘要", "p1", t(21))

    def test_cannot_transfer_resolved_issue(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))
        self.dossier.sign_plan("plan-1", "p1", t(6))
        self.dossier.sign_plan("plan-1", "p2", t(6))
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_DONE, t(9), by="王调解员"
        )
        with self.assertRaisesRegex(DossierError, "已写入调解书"):
            self.dossier.transfer_to_litigation(t(10), issue_ids=["i1"])

    # ----- 脱敏公开查询 -----------------------------------------------

    def test_public_progress_is_redacted(self) -> None:
        self.dossier.add_participant(
            "p3",
            "林小苗",
            ParticipantRole.THIRD_PARTY,
            t(1, 14),
            is_minor=True,
            family_privacy=True,
        )
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(5))
        self.dossier.sign_plan("plan-1", "p1", t(6))
        self.dossier.sign_plan("plan-1", "p2", t(6))
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_PARTIAL, t(15), by="王调解员"
        )
        self.dossier.submit_evidence("e1", "林权证", "四至：东界沟", "p1", t(3))
        self.dossier.submit_evidence("e2", "迟到录音", "村会计复述", "p2", t(12), issue_id="i2")
        self.dossier.transfer_to_litigation(t(20), handover_note="租金移交法庭")

        public = self.dossier.public_progress()
        rendered = repr(public)
        for secret in ("林大山", "林小苗", "合作社", "东界沟", "杉木林", "租金差额", "四至"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(public["participants"][0]["code"], "当事人1")
        self.assertEqual(public["participants"][1]["code"], "单位2")
        self.assertEqual(public["participants"][2]["code"], "未成年人1")
        statuses = {item["code"]: item["status"] for item in public["issues"]}
        self.assertEqual(statuses["争点1"], "部分和解")
        self.assertEqual(statuses["争点2"], "已移交诉讼")
        self.assertEqual(public["evidence_count"], 2)
        self.assertEqual(public["late_evidence_count"], 1)
        self.assertEqual(public["status"], "已移交诉讼")

    # ----- 结案复核回放 -----------------------------------------------

    def test_closing_review_replays_who_agreed_what_when_and_handover(self) -> None:
        self.dossier.submit_evidence("e1", "林权证", "四至：东界沟", "p1", t(3), issue_id="i1")
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        self.dossier.withdraw_consensus(
            "i1", t(5), by="林大山", reason="界沟位置有异议"
        )
        self.dossier.submit_evidence("e2", "老照片", "1998 年影像", "p1", t(8), issue_id="i1")
        self.dossier.reach_consensus("i1", "边界以老石埂为准并栽桩", t(9), "m1")
        self.dossier.seal_plan("plan-1", ["i1"], t(10, 10))
        self.dossier.sign_plan("plan-1", "p1", t(11))
        self.dossier.sign_plan("plan-1", "p2", t(11))
        self.dossier.record_performance(
            "plan-1", "i1", PERFORMANCE_PARTIAL, t(15), by="王调解员", note="东段完成"
        )
        locked = self.dossier.transfer_to_litigation(
            t(20), handover_note="租金部分随卷移交"
        )

        review = self.dossier.closing_review()
        boundary, rent = review["issues"]

        # 谁在何时同意了什么：两个版本、撤回人原因、签收人全部可回放。
        self.assertEqual(boundary["type"], "边界")
        self.assertEqual(boundary["sealed_consensus_version"], 2)
        self.assertEqual(len(boundary["consensus_history"]), 2)
        v1 = boundary["consensus_history"][0]
        self.assertEqual(v1["terms"], "边界以界沟为准")
        self.assertEqual(v1["withdrawn"]["by"], "林大山")
        self.assertEqual({party["name"] for party in v1["agreed_by"]}, {"林大山", "合作社"})
        self.assertEqual(boundary["consensus_history"][1]["terms"], "边界以老石埂为准并栽桩")
        self.assertEqual({item["signed"] for item in boundary["signoffs"]}, {True})
        self.assertEqual(boundary["performance"][-1]["status"], PERFORMANCE_PARTIAL)
        self.assertEqual(boundary["final"], "部分和解")

        # 未解决部分（租金）明确列入移交，锁定版本号可追溯；边界争点
        # 已部分和解，按调解书继续履行，不随诉讼移交。
        self.assertEqual(rent["final"], "已移交诉讼")
        self.assertEqual(review["unresolved_issue_ids"], ["i1", "i2"])
        self.assertEqual(review["transfer"]["issue_ids"], ["i2"])
        self.assertEqual(review["transfer"]["locked_seq"], locked["locked_seq"])

        # 时间线从受理到移交完整有序。
        titles = [item["title"] for item in review["timeline"]]
        self.assertEqual(titles[0], "受理立案")
        self.assertIn("达成共识（边界 v1）", titles)
        self.assertIn("撤回共识（v1）", titles)
        self.assertIn("达成共识（边界 v2）", titles)
        self.assertEqual(titles[-1], "转入诉讼并锁定卷宗")
        self.assertTrue(all(prev["seq"] < nxt["seq"]
                            for prev, nxt in zip(review["timeline"], review["timeline"][1:])))

    def test_event_log_is_append_only(self) -> None:
        self.dossier.open_session("m1", "王调解员", t(4))
        self.dossier.reach_consensus("i1", "边界以界沟为准", t(4, 10), "m1")
        events = self.dossier.events
        with self.assertRaises(AttributeError):
            events.append(None)  # type: ignore[attr-defined]
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))


if __name__ == "__main__":
    unittest.main()
