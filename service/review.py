"""结案复核：按版本回放调解卷全过程。

复核输出回答三个问题：
1. 每个争点的共识在何时、依据哪个方案版本、由哪些当事人同意，是否撤回；
2. 方案签收与部分履行的时间线；
3. 未解决争点在移交诉讼时锁定了哪个卷宗版本、随卷移交了哪些证据与陈述。
"""

from __future__ import annotations

from service.domain import (
    MediationService,
    PERFORMANCE_COMPLETE,
    PERFORMANCE_PARTIAL,
    SIGN_SIGNED,
    SIGN_REFUSED,
    SIGN_DEEMED,
    verify_hash_chain,
)

SIGN_LABELS = {
    SIGN_SIGNED: "签收",
    SIGN_REFUSED: "拒签",
    SIGN_DEEMED: "留置送达",
}

PERFORMANCE_LABELS = {
    PERFORMANCE_PARTIAL: "部分履行",
    PERFORMANCE_COMPLETE: "履行完毕",
}

_EVENT_DESCRIPTIONS = {
    "case_accepted": "受理立案",
    "party_registered": "登记当事人",
    "agent_authorized": "授权代理人",
    "agent_revoked": "代理人授权失效",
    "issue_opened": "登记争点",
    "statement_recorded": "记录陈述或补充材料",
    "evidence_submitted": "收录证据摘要",
    "session_started": "开始调解会议",
    "session_concluded": "结束调解会议",
    "plan_proposed": "提出调解方案",
    "consensus_reached": "争点达成共识",
    "consensus_withdrawn": "撤回争点共识",
    "plan_signed": "记录方案签收",
    "performance_recorded": "记录方案履行",
    "litigation_transferred": "未决部分移交诉讼",
    "case_closed": "结案",
}


def build_review(
    service: MediationService, case_id: str, version: int | None = None
) -> dict:
    """生成结构化复核报告；version 可锁定到历史卷宗版本回放。"""

    current = service.load(case_id)
    view = service.load_at(case_id, version) if version is not None else current
    events = list(view.events)

    integrity_ok = verify_hash_chain(current.events)
    if view is not current:
        integrity_ok = integrity_ok and verify_hash_chain(events)

    agreements = _agreements(view)
    timeline = [_timeline_entry(view, event) for event in events]

    report = {
        "case_id": case_id,
        "title": view.title,
        "replayed_version": view.version,
        "current_version": current.version,
        "locked_review": version is not None and version != current.version,
        "integrity_ok": integrity_ok,
        "status": view.status,
        "accepted_at": view.accepted_at,
        "closed_at": view.closed_at,
        "timeline": timeline,
        "agreements": agreements,
        "signoffs": _signoffs(view),
        "performance": _performance(view),
        "resolved_issues": [issue.issue_id for issue in view.resolved_issues()],
        "unresolved_issues": [
            {
                "issue_id": issue.issue_id,
                "kind": issue.kind,
                "title": issue.title,
                "last_consensus_state": (
                    "已撤回" if issue.consensus else "从未达成一致"
                ),
            }
            for issue in view.unresolved_issues()
        ],
        "litigation_transfer": _transfer(view),
    }
    return report


def _timeline_entry(view, event) -> dict:
    entry = {
        "seq": event.seq,
        "at": event.at,
        "actor_id": event.actor_id,
        "type": event.type,
        "description": _EVENT_DESCRIPTIONS.get(event.type, event.type),
    }
    data = event.data
    if event.type == "consensus_reached":
        entry["detail"] = {
            "issue_id": data["issue_id"],
            "issue_title": view.issues[data["issue_id"]].title,
            "plan": f"{data['plan_id']} v{data['version']}",
            "consenters": [view.maker_name(pid) for pid in data["consenters"]],
        }
    elif event.type == "consensus_withdrawn":
        entry["detail"] = {
            "issue_id": data["issue_id"],
            "issue_title": view.issues[data["issue_id"]].title,
            "by": view.maker_name(data["by_party_id"]) if data.get("by_party_id") else None,
            "reason": data.get("reason", ""),
        }
    elif event.type == "evidence_submitted":
        entry["detail"] = {
            "evidence_id": data["evidence_id"],
            "issue_id": data["issue_id"],
            "late": data["late"],
            "private": data.get("private", False),
        }
    elif event.type == "plan_proposed":
        entry["detail"] = {
            "plan": f"{data['plan_id']} v{data['version']}",
            "basis_version": data["basis_version"],
            "basis_evidence_ids": data["basis_evidence_ids"],
        }
    elif event.type == "litigation_transferred":
        entry["detail"] = {
            "locked_version": data["locked_version"],
            "unresolved_issues": [item["issue_id"] for item in data["unresolved_issues"]],
        }
    return entry


def _agreements(view) -> list[dict]:
    result = []
    for issue in view.issues.values():
        for record in issue.consensus:
            result.append(
                {
                    "issue_id": issue.issue_id,
                    "issue_title": issue.title,
                    "kind": issue.kind,
                    "plan_id": record.plan_id,
                    "plan_version": record.plan_version,
                    "agreed_at": record.at,
                    "agreed_by_actor": record.actor_id,
                    "consenters": [view.maker_name(pid) for pid in record.consenters],
                    "state": "active" if record.active else "withdrawn",
                    "withdrawn_at": record.withdrawn_at,
                    "withdrawn_by": (
                        view.maker_name(record.withdrawn_by)
                        if record.withdrawn_by
                        else None
                    ),
                    "withdraw_reason": record.withdraw_reason,
                }
            )
    return result


def _signoffs(view) -> list[dict]:
    return [
        {
            "plan": f"{item.plan_id} v{item.plan_version}",
            "party": view.maker_name(item.party_id),
            "status": item.status,
            "status_label": SIGN_LABELS.get(item.status, item.status),
            "at": item.at,
        }
        for item in view.signoffs
    ]


def _performance(view) -> list[dict]:
    return [
        {
            "issue_id": item.issue_id,
            "issue_title": view.issues[item.issue_id].title,
            "status": item.status,
            "status_label": PERFORMANCE_LABELS.get(item.status, item.status),
            "progress": item.progress,
            "note": item.note,
            "at": item.at,
        }
        for item in view.performance
    ]


def _transfer(view) -> dict | None:
    if view.transfer is None:
        return None
    data = view.transfer
    return {
        "at": data.get("at"),
        "locked_version": data.get("locked_version"),
        "reason": data.get("reason", ""),
        "unresolved_issues": data.get("unresolved_issues", []),
        "handover_evidence_ids": data.get("handover_evidence_ids", []),
        "handover_statement_ids": data.get("handover_statement_ids", []),
        "handover_note": data.get("handover_note", ""),
    }
