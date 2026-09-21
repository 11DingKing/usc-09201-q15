"""公开查询的脱敏层。

调解卷对内（司法所、林业站调解员）完整可读；对公众只返回“进度”：
有哪些类型的争点、各争点处于什么阶段、方案履行百分比、是否已移交
诉讼或结案。主体身份、证件、联系方式、具体坐落、陈述内容、证据摘
要以及未成年人与家庭隐私标签一律不出现在公开结果中。
"""

from __future__ import annotations

from service.domain import (
    PERFORMANCE_COMPLETE,
    PERFORMANCE_PARTIAL,
    STATUS_ACCEPTED,
    STATUS_CLOSED,
    STATUS_MEDIATING,
    STATUS_PARTIAL,
    STATUS_TRANSFERRED,
    CaseView,
    Issue,
)

ISSUE_KIND_LABELS = {
    "boundary": "边界争议",
    "rent": "租金争议",
    "oral_agreement": "历史口头约定争议",
    "other": "其他争议",
}

STATUS_LABELS = {
    STATUS_ACCEPTED: "已受理",
    STATUS_MEDIATING: "调解中",
    STATUS_PARTIAL: "部分达成一致",
    STATUS_TRANSFERRED: "未决部分已移交诉讼",
    STATUS_CLOSED: "已结案",
}

# 公开进度中允许出现的顶层字段白名单（其余一律不下发）。
_PUBLIC_TOP_FIELDS = {
    "case_ref",
    "status",
    "status_label",
    "issues",
    "resolved_count",
    "unresolved_count",
    "plan_progress",
    "late_evidence_count",
    "litigation_transferred",
    "closed",
}


def mask_name(name: str) -> str:
    """姓名脱敏：保留首字，其余以 * 代替；机构名保留首尾。"""

    if not name:
        return "**"
    if len(name) == 1:
        return "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def mask_id_number(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def mask_contact(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def mask_location(location: dict) -> dict:
    """坐落只保留到行政村/林班粒度，隐去小地名、四至与坐标。"""

    allowed = {"village", "compartment"}
    return {key: value for key, value in location.items() if key in allowed}


def _issue_stage(view: CaseView, issue: Issue) -> str:
    active = issue.active_consensus()
    if active is not None:
        performance = view.current_performance(issue.issue_id)
        if performance is not None:
            if performance.status == PERFORMANCE_COMPLETE:
                return "履行完毕"
            if performance.status == PERFORMANCE_PARTIAL:
                return "部分履行"
        return "已达成一致"
    if issue.consensus:
        return "共识已撤回"
    if any(session.issue_ids and issue.issue_id in session.issue_ids for session in view.sessions):
        return "调解中"
    return "待调解"


def _issue_progress(view: CaseView, issue: Issue) -> dict:
    result = {
        "kind": issue.kind,
        "kind_label": ISSUE_KIND_LABELS.get(issue.kind, "争议事项"),
        "stage": _issue_stage(view, issue),
    }
    performance = view.current_performance(issue.issue_id)
    if performance is not None and performance.progress is not None:
        result["progress_percent"] = performance.progress
    return result


def build_public_progress(view: CaseView, case_ref: str) -> dict:
    """构造对外公开的脱敏进度。

    case_ref 使用专门的查询编号而非内部卷宗号，避免编号被枚举。
    """

    issues = [_issue_progress(view, issue) for issue in view.issues.values()]
    resolved = len(view.resolved_issues())
    payload = {
        "case_ref": case_ref,
        "status": view.status,
        "status_label": STATUS_LABELS.get(view.status, view.status),
        "issues": issues,
        "resolved_count": resolved,
        "unresolved_count": len(view.issues) - resolved,
        # 只报数量与“存在迟到证据”这一事实，不报内容。
        "late_evidence_count": sum(1 for item in view.evidence if item.late),
        "litigation_transferred": view.status == STATUS_TRANSFERRED
        or view.transfer is not None,
        "closed": view.status == STATUS_CLOSED,
    }
    if payload["issues"]:
        percents = [
            item["progress_percent"]
            for item in payload["issues"]
            if "progress_percent" in item
        ]
        if percents:
            payload["plan_progress"] = round(sum(percents) / len(payload["issues"]))
    return {key: value for key, value in payload.items() if key in _PUBLIC_TOP_FIELDS}


def assert_public_payload_safe(payload: dict) -> None:
    """测试与上线前自检：公开结果不得混入任何敏感字段。"""

    forbidden = {
        "parties",
        "agents",
        "statements",
        "evidence",
        "sessions",
        "plans",
        "signoffs",
        "location",
        "id_number",
        "contact",
        "privacy_tags",
        "minor",
        "guardian_party_id",
        "summary",
        "description",
        "content",
        "materials",
        "handover_note",
    }
    leaked = forbidden.intersection(_walk_keys(payload))
    if leaked:
        raise AssertionError(f"公开进度包含敏感字段：{sorted(leaked)}")


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_walk_keys(item))
    return keys
