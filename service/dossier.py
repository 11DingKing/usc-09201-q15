"""林地争议调解卷领域模型。

调解卷由司法所与林业站共同建设，分层保存：主体（当事人与代理人
授权）、争点、证据摘要、调解方案与签收状态。所有业务动作都只向
事件日志追加事件，旧记录永远不被覆盖：

- 当事人补充陈述只新增陈述版本；更换代理人只新增授权记录，旧陈述
  仍归属于作出时的代理人；
- 每个争点可单独达成或撤回共识，共识按争点内版本编号，撤回后
  历史保留，可基于新证据再次达成新版本；
- 每次共识自动快照当时已到达的证据序号（迟到证据不会改写旧版
  本）；调解书封存时锁定所引用的共识版本；
- 转入诉讼时锁定整卷版本号，未解决争点随卷移交，此后全卷冻结；
- 对外查询只能得到脱敏进度，不暴露自然人姓名、未成年人身份线索、
  陈述与证据内容。

结案复核通过 :meth:`Dossier.closing_review` 与
:meth:`Dossier.timeline` 回放全过程，核对谁在何时同意了什么、
哪些部分已和解、未解决部分如何移交。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class DossierError(ValueError):
    """违反调解卷业务规则时抛出。"""


class IssueType(StrEnum):
    """争点类型：经营权流转后最常见的三类争议。"""

    BOUNDARY = "边界"
    RENT = "租金"
    VERBAL = "历史口头约定"


class ParticipantRole(StrEnum):
    APPLICANT = "申请人"
    RESPONDENT = "被申请人"
    THIRD_PARTY = "第三人"


PERFORMANCE_PENDING = "未履行"
PERFORMANCE_PARTIAL = "部分履行"
PERFORMANCE_DONE = "已履行"
_PERFORMANCE_STATUSES = {
    PERFORMANCE_PENDING,
    PERFORMANCE_PARTIAL,
    PERFORMANCE_DONE,
}

_DISPUTING_ROLES = {ParticipantRole.APPLICANT, ParticipantRole.RESPONDENT}


@dataclass(frozen=True)
class Event:
    """不可变事件：append-only 日志中的一条记录。"""

    seq: int
    at: datetime
    actor: str
    kind: str
    data: Mapping[str, Any]


@dataclass
class _Consensus:
    version: int
    terms: str
    agreed_by: tuple[str, ...]
    at: datetime
    session_id: str
    seq: int
    basis_evidence_seqs: tuple[int, ...]
    withdrawn: dict[str, Any] | None = None


@dataclass
class _Issue:
    id: str
    type: str
    summary: str
    opened_at: datetime
    consensus: list[_Consensus] = field(default_factory=list)


@dataclass
class _Plan:
    id: str
    sealed_at: datetime
    seq: int
    issue_versions: dict[str, int]
    signoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    performance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class _View:
    seq: int = 0
    dossier_id: str = ""
    accepted_at: datetime | None = None
    evidence_deadline: datetime | None = None
    participants: dict[str, dict[str, Any]] = field(default_factory=dict)
    participant_order: list[str] = field(default_factory=list)
    issues: dict[str, _Issue] = field(default_factory=dict)
    issue_order: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    statements: list[dict[str, Any]] = field(default_factory=list)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    plans: dict[str, _Plan] = field(default_factory=dict)
    transfer: dict[str, Any] | None = None


def _apply(view: _View, event: Event) -> None:
    data = event.data
    kind = event.kind

    if kind == "dossier_accepted":
        view.dossier_id = data["dossier_id"]
        view.accepted_at = event.at
        view.evidence_deadline = data.get("evidence_deadline")
    elif kind == "participant_added":
        view.participants[data["participant_id"]] = {
            "id": data["participant_id"],
            "name": data["name"],
            "role": data["role"],
            "kind": data["kind"],
            "is_minor": data["is_minor"],
            "family_privacy": data["family_privacy"],
            "added_at": event.at,
            "authorizations": [],
        }
        view.participant_order.append(data["participant_id"])
    elif kind == "agent_changed":
        participant = view.participants[data["participant_id"]]
        authorizations = participant["authorizations"]
        if authorizations and authorizations[-1].get("ended_at") is None:
            authorizations[-1]["ended_at"] = event.at
        authorizations.append(
            {
                "agent_name": data["agent_name"],
                "valid_from": event.at,
                "basis": data["basis"],
                "seq": event.seq,
                "ended_at": None,
            }
        )
    elif kind == "agent_revoked":
        participant = view.participants[data["participant_id"]]
        if authorizations := participant["authorizations"]:
            if authorizations[-1].get("ended_at") is None:
                authorizations[-1]["ended_at"] = event.at
    elif kind == "issue_opened":
        issue = _Issue(
            id=data["issue_id"],
            type=data["type"],
            summary=data["summary"],
            opened_at=event.at,
        )
        view.issues[data["issue_id"]] = issue
        view.issue_order.append(data["issue_id"])
    elif kind == "evidence_submitted":
        view.evidence.append(
            {
                "id": data["evidence_id"],
                "issue_id": data.get("issue_id"),
                "title": data["title"],
                "summary": data["summary"],
                "submitted_by": data["submitted_by"],
                "at": event.at,
                "late": data["late"],
                "seq": event.seq,
            }
        )
    elif kind == "statement_made":
        view.statements.append(
            {
                "id": data["statement_id"],
                "participant_id": data["participant_id"],
                "issue_id": data.get("issue_id"),
                "content": data["content"],
                "agent_name": data.get("agent_name"),
                "at": event.at,
                "seq": event.seq,
            }
        )
    elif kind == "session_opened":
        view.sessions[data["session_id"]] = {
            "id": data["session_id"],
            "mediator": data["mediator"],
            "venue": data.get("venue", ""),
            "started_at": event.at,
            "ended_at": None,
        }
    elif kind == "session_closed":
        view.sessions[data["session_id"]]["ended_at"] = event.at
    elif kind == "consensus_reached":
        issue = view.issues[data["issue_id"]]
        issue.consensus.append(
            _Consensus(
                version=data["version"],
                terms=data["terms"],
                agreed_by=tuple(data["agreed_by"]),
                at=event.at,
                session_id=data["session_id"],
                seq=event.seq,
                basis_evidence_seqs=tuple(data["basis_evidence_seqs"]),
            )
        )
    elif kind == "consensus_withdrawn":
        issue = view.issues[data["issue_id"]]
        record = issue.consensus[data["version"] - 1]
        record.withdrawn = {
            "by": data["by"],
            "reason": data["reason"],
            "at": event.at,
            "seq": event.seq,
        }
    elif kind == "plan_sealed":
        view.plans[data["plan_id"]] = _Plan(
            id=data["plan_id"],
            sealed_at=event.at,
            seq=event.seq,
            issue_versions=dict(data["issue_versions"]),
        )
    elif kind == "plan_signed":
        plan = view.plans[data["plan_id"]]
        plan.signoffs[data["participant_id"]] = {
            "participant_id": data["participant_id"],
            "signed": data["signed"],
            "reason": data.get("reason", ""),
            "agent_name": data.get("agent_name"),
            "at": event.at,
            "seq": event.seq,
        }
    elif kind == "performance_recorded":
        plan = view.plans[data["plan_id"]]
        plan.performance.setdefault(data["issue_id"], []).append(
            {
                "status": data["status"],
                "by": data["by"],
                "note": data.get("note", ""),
                "at": event.at,
                "seq": event.seq,
            }
        )
    elif kind == "transferred_to_litigation":
        view.transfer = {
            "issue_ids": tuple(data["issue_ids"]),
            "handover_note": data.get("handover_note", ""),
            "by": data["by"],
            "at": event.at,
            "locked_seq": data["locked_seq"],
            "seq": event.seq,
        }
    else:  # pragma: no cover - 新增事件类型时应在此分支补充投影
        raise DossierError(f"未知事件类型：{kind}")


def _replay(events: list[Event]) -> _View:
    view = _View()
    for event in events:
        _apply(view, event)
        view.seq = event.seq
    return view


class Dossier:
    """一卷林地争议调解案件。

    时间由调用方显式传入（便于复核与测试），所有时间按“事件发生
    时间”记录，而不是系统处理时间。
    """

    def __init__(
        self,
        dossier_id: str,
        accepted_at: datetime,
        *,
        accepted_by: str = "司法所",
        evidence_deadline: datetime | None = None,
    ) -> None:
        self._events: list[Event] = []
        self._append(
            accepted_at,
            accepted_by,
            "dossier_accepted",
            {"dossier_id": dossier_id, "evidence_deadline": evidence_deadline},
        )

    # ----- 内部工具 ---------------------------------------------------

    def _view(self) -> _View:
        return _replay(self._events)

    def _append(self, at: datetime, actor: str, kind: str, data: Mapping[str, Any]) -> Event:
        event = Event(seq=len(self._events) + 1, at=at, actor=actor, kind=kind, data=dict(data))
        # 先投影校验，再落日志：投影失败说明事件不被当前状态接受。
        trial = _replay(self._events)
        _apply(trial, event)
        self._events.append(event)
        return event

    def _guard_open(self, view: _View, action: str) -> None:
        if view.transfer is not None:
            raise DossierError(
                f"不能{action}：调解卷已于 {view.transfer['at']:%Y-%m-%d %H:%M} "
                f"随版本 {view.transfer['locked_seq']} 转入诉讼并锁定"
            )

    def _require_participant(self, view: _View, participant_id: str) -> dict[str, Any]:
        try:
            return view.participants[participant_id]
        except KeyError:
            raise DossierError(f"主体不存在：{participant_id}") from None

    def _require_issue(self, view: _View, issue_id: str) -> _Issue:
        try:
            return view.issues[issue_id]
        except KeyError:
            raise DossierError(f"争点不存在：{issue_id}") from None

    @staticmethod
    def _active_consensus(issue: _Issue) -> _Consensus | None:
        for record in reversed(issue.consensus):
            if record.withdrawn is None:
                return record
        return None

    def _plan_for_issue(self, view: _View, issue_id: str) -> _Plan | None:
        for plan in view.plans.values():
            if issue_id in plan.issue_versions:
                return plan
        return None

    def _disputing_parties(self, view: _View) -> list[str]:
        return [
            pid
            for pid in view.participant_order
            if view.participants[pid]["role"] in _DISPUTING_ROLES
        ]

    def _agent_at(self, participant: dict[str, Any], at: datetime) -> dict[str, Any] | None:
        for authorization in reversed(participant["authorizations"]):
            if authorization["valid_from"] <= at and (
                authorization["ended_at"] is None or authorization["ended_at"] > at
            ):
                return authorization
        return None

    @staticmethod
    def _name(view: _View, participant_id: str) -> str:
        return view.participants[participant_id]["name"]

    # ----- 主体层：当事人与授权 --------------------------------------

    def add_participant(
        self,
        participant_id: str,
        name: str,
        role: ParticipantRole,
        at: datetime,
        *,
        kind: str = "个人",
        is_minor: bool = False,
        family_privacy: bool = False,
        actor: str = "司法所",
    ) -> None:
        """登记主体。未成年人与家庭隐私标记会在公开查询中强制脱敏。"""

        view = self._view()
        self._guard_open(view, "登记主体")
        if participant_id in view.participants:
            raise DossierError(f"主体已存在：{participant_id}")
        self._append(
            at,
            actor,
            "participant_added",
            {
                "participant_id": participant_id,
                "name": name,
                "role": str(role),
                "kind": kind,
                "is_minor": is_minor,
                "family_privacy": family_privacy,
            },
        )

    def change_agent(
        self,
        participant_id: str,
        agent_name: str,
        valid_from: datetime,
        *,
        basis: str = "",
        actor: str = "司法所",
    ) -> None:
        """更换代理人：旧授权在新授权生效时终止，旧陈述归属不变。"""

        view = self._view()
        self._guard_open(view, "变更授权")
        self._require_participant(view, participant_id)
        self._append(
            valid_from,
            actor,
            "agent_changed",
            {
                "participant_id": participant_id,
                "agent_name": agent_name,
                "basis": basis,
            },
        )

    def revoke_agent(
        self, participant_id: str, at: datetime, *, actor: str = "司法所"
    ) -> None:
        """撤销代理，当事人恢复自行参与。"""

        view = self._view()
        self._guard_open(view, "撤销代理")
        participant = self._require_participant(view, participant_id)
        if self._agent_at(participant, at) is None:
            raise DossierError(f"主体 {participant_id} 当前没有生效中的代理授权")
        self._append(
            at,
            actor,
            "agent_revoked",
            {"participant_id": participant_id},
        )

    # ----- 争点层 -----------------------------------------------------

    def open_issue(
        self,
        issue_id: str,
        type: IssueType,
        summary: str,
        at: datetime,
        *,
        actor: str = "司法所",
    ) -> None:
        """登记争点（边界、租金、历史口头约定等）。"""

        view = self._view()
        self._guard_open(view, "登记争点")
        if issue_id in view.issues:
            raise DossierError(f"争点已存在：{issue_id}")
        self._append(
            at,
            actor,
            "issue_opened",
            {"issue_id": issue_id, "type": str(type), "summary": summary},
        )

    # ----- 证据摘要层 -------------------------------------------------

    def submit_evidence(
        self,
        evidence_id: str,
        title: str,
        summary: str,
        submitted_by: str,
        at: datetime,
        *,
        issue_id: str | None = None,
        actor: str = "司法所",
    ) -> bool:
        """提交证据摘要。晚于举证期限到达的证据标记为迟到证据。

        迟到证据照常入卷，但不会改动既有共识所依据的证据快照。
        返回是否迟到。
        """

        view = self._view()
        self._guard_open(view, "提交证据")
        if any(item["id"] == evidence_id for item in view.evidence):
            raise DossierError(f"证据已存在：{evidence_id}")
        if issue_id is not None:
            self._require_issue(view, issue_id)
        late = view.evidence_deadline is not None and at > view.evidence_deadline
        self._append(
            at,
            actor,
            "evidence_submitted",
            {
                "evidence_id": evidence_id,
                "issue_id": issue_id,
                "title": title,
                "summary": summary,
                "submitted_by": submitted_by,
                "late": late,
            },
        )
        return late

    def record_statement(
        self,
        statement_id: str,
        participant_id: str,
        content: str,
        at: datetime,
        *,
        issue_id: str | None = None,
        via_agent: bool = False,
        actor: str = "司法所",
    ) -> None:
        """记录当事人陈述。补充材料只追加新版本，旧陈述永不覆盖。

        ``via_agent`` 为真时代理人必须在该时点持有有效授权；代理人
        姓名随陈述快照保存，事后更换代理人不改变旧陈述的归属。
        """

        view = self._view()
        self._guard_open(view, "记录陈述")
        participant = self._require_participant(view, participant_id)
        if issue_id is not None:
            self._require_issue(view, issue_id)
        if any(item["id"] == statement_id for item in view.statements):
            raise DossierError(f"陈述已存在：{statement_id}")
        agent_name = None
        if via_agent:
            authorization = self._agent_at(participant, at)
            if authorization is None:
                raise DossierError(
                    f"主体 {participant_id} 在 {at:%Y-%m-%d %H:%M} 没有生效中的代理授权"
                )
            agent_name = authorization["agent_name"]
        self._append(
            at,
            actor,
            "statement_made",
            {
                "statement_id": statement_id,
                "participant_id": participant_id,
                "issue_id": issue_id,
                "content": content,
                "agent_name": agent_name,
            },
        )

    # ----- 调解会话（允许并行）---------------------------------------

    def open_session(
        self,
        session_id: str,
        mediator: str,
        started_at: datetime,
        *,
        venue: str = "",
        actor: str = "司法所",
    ) -> None:
        """开启一次调解会话；多个会话允许时间重叠（并行调解）。"""

        view = self._view()
        self._guard_open(view, "开启调解会话")
        if session_id in view.sessions:
            raise DossierError(f"调解会话已存在：{session_id}")
        self._append(
            started_at,
            actor,
            "session_opened",
            {"session_id": session_id, "mediator": mediator, "venue": venue},
        )

    def close_session(
        self, session_id: str, ended_at: datetime, *, actor: str = "司法所"
    ) -> None:
        """结束调解会话。"""

        view = self._view()
        self._guard_open(view, "结束调解会话")
        session = view.sessions.get(session_id)
        if session is None:
            raise DossierError(f"调解会话不存在：{session_id}")
        if session["ended_at"] is not None:
            raise DossierError(f"调解会话已结束：{session_id}")
        self._append(
            ended_at,
            actor,
            "session_closed",
            {"session_id": session_id},
        )

    # ----- 逐争点共识：达成与撤回 ------------------------------------

    def reach_consensus(
        self,
        issue_id: str,
        terms: str,
        at: datetime,
        session_id: str,
        *,
        agreed_by: list[str] | tuple[str, ...] | None = None,
        actor: str = "司法所",
    ) -> int:
        """就单个争点达成共识，返回该争点内的共识版本号。

        共识必须发生在一场进行中的调解会话里；争议双方全体同意方可
        成立。达成时快照与该争点相关（含卷内通用证据）的全部已到达
        证据序号，供日后核对“当时依据了哪些证据”。
        """

        view = self._view()
        self._guard_open(view, "达成共识")
        issue = self._require_issue(view, issue_id)
        session = view.sessions.get(session_id)
        if session is None:
            raise DossierError(f"调解会话不存在：{session_id}")
        if not (session["started_at"] <= at and self._session_open_at(session, at)):
            raise DossierError(f"共识时间 {at:%Y-%m-%d %H:%M} 不在会话 {session_id} 进行期间")
        if self._plan_for_issue(view, issue_id) is not None:
            raise DossierError(f"争点 {issue_id} 已写入封存的调解书，不能再达成新共识")
        parties = list(agreed_by) if agreed_by is not None else self._disputing_parties(view)
        required = set(self._disputing_parties(view))
        if not required or set(parties) != required:
            raise DossierError("共识须由争议双方全体主体同意")
        basis = tuple(
            item["seq"]
            for item in view.evidence
            if item["issue_id"] is None or item["issue_id"] == issue_id
        )
        version = len(issue.consensus) + 1
        self._append(
            at,
            actor,
            "consensus_reached",
            {
                "issue_id": issue_id,
                "version": version,
                "terms": terms,
                "agreed_by": parties,
                "session_id": session_id,
                "basis_evidence_seqs": basis,
            },
        )
        return version

    @staticmethod
    def _session_open_at(session: dict[str, Any], at: datetime) -> bool:
        return session["ended_at"] is None or session["ended_at"] >= at

    def withdraw_consensus(
        self,
        issue_id: str,
        at: datetime,
        *,
        by: str,
        reason: str,
        actor: str = "司法所",
    ) -> int:
        """撤回该争点当前生效的共识，返回被撤回的版本号。

        撤回不删除记录；已被封存调解书引用的共识不允许撤回。
        """

        view = self._view()
        self._guard_open(view, "撤回共识")
        issue = self._require_issue(view, issue_id)
        active = self._active_consensus(issue)
        if active is None:
            raise DossierError(f"争点 {issue_id} 当前没有生效中的共识")
        if self._plan_for_issue(view, issue_id) is not None:
            raise DossierError(f"争点 {issue_id} 的共识已写入封存的调解书，不能撤回")
        self._append(
            at,
            actor,
            "consensus_withdrawn",
            {"issue_id": issue_id, "version": active.version, "by": by, "reason": reason},
        )
        return active.version

    # ----- 调解方案层与签收状态 ---------------------------------------

    def seal_plan(
        self,
        plan_id: str,
        issue_ids: list[str] | tuple[str, ...],
        at: datetime,
        *,
        actor: str = "司法所",
    ) -> None:
        """封存调解方案：逐争点锁定当前共识版本，形成调解书。"""

        view = self._view()
        self._guard_open(view, "封存调解方案")
        if plan_id in view.plans:
            raise DossierError(f"调解方案已存在：{plan_id}")
        if not issue_ids:
            raise DossierError("调解方案至少要包含一个争点")
        issue_versions: dict[str, int] = {}
        for issue_id in issue_ids:
            issue = self._require_issue(view, issue_id)
            if self._plan_for_issue(view, issue_id) is not None:
                raise DossierError(f"争点 {issue_id} 已存在封存的调解方案")
            active = self._active_consensus(issue)
            if active is None:
                raise DossierError(f"争点 {issue_id} 没有生效中的共识，不能写入调解方案")
            issue_versions[issue_id] = active.version
        self._append(
            at,
            actor,
            "plan_sealed",
            {"plan_id": plan_id, "issue_versions": issue_versions},
        )

    def sign_plan(
        self,
        plan_id: str,
        participant_id: str,
        at: datetime,
        *,
        signed: bool = True,
        via_agent: bool = False,
        reason: str = "",
        actor: str = "司法所",
    ) -> None:
        """当事人对调解书签收（或拒绝签收）。签收状态不可更改。"""

        view = self._view()
        self._guard_open(view, "签收调解方案")
        plan = view.plans.get(plan_id)
        if plan is None:
            raise DossierError(f"调解方案不存在：{plan_id}")
        participant = self._require_participant(view, participant_id)
        if participant_id in plan.signoffs:
            raise DossierError(f"主体 {participant_id} 已完成签收，签收状态不可覆盖")
        agent_name = None
        if via_agent:
            authorization = self._agent_at(participant, at)
            if authorization is None:
                raise DossierError(
                    f"主体 {participant_id} 在 {at:%Y-%m-%d %H:%M} 没有生效中的代理授权"
                )
            agent_name = authorization["agent_name"]
        self._append(
            at,
            actor,
            "plan_signed",
            {
                "plan_id": plan_id,
                "participant_id": participant_id,
                "signed": signed,
                "reason": reason,
                "agent_name": agent_name,
            },
        )

    def record_performance(
        self,
        plan_id: str,
        issue_id: str,
        status: str,
        at: datetime,
        *,
        by: str,
        note: str = "",
        actor: str = "司法所",
    ) -> None:
        """记录方案条款履行情况：未履行 / 部分履行 / 已履行。

        每次记录都追加保留，支持从部分履行推进到履行完毕的全过程。
        """

        view = self._view()
        self._guard_open(view, "记录履行情况")
        plan = view.plans.get(plan_id)
        if plan is None:
            raise DossierError(f"调解方案不存在：{plan_id}")
        if issue_id not in plan.issue_versions:
            raise DossierError(f"争点 {issue_id} 不属于方案 {plan_id}")
        if status not in _PERFORMANCE_STATUSES:
            raise DossierError(f"未知履行状态：{status}")
        self._append(
            at,
            actor,
            "performance_recorded",
            {
                "plan_id": plan_id,
                "issue_id": issue_id,
                "status": status,
                "by": by,
                "note": note,
            },
        )

    # ----- 转入诉讼：版本锁定与移交 -----------------------------------

    def transfer_to_litigation(
        self,
        at: datetime,
        *,
        issue_ids: list[str] | tuple[str, ...] | None = None,
        by: str = "司法所",
        handover_note: str = "",
        actor: str = "司法所",
    ) -> dict[str, Any]:
        """将未解决争点转入诉讼并锁定整卷当前版本，此后全卷冻结。

        未指定争点时移交全部未解决争点。返回含锁定版本号的移交记录。
        """

        view = self._view()
        self._guard_open(view, "转入诉讼")
        # 默认只移交尚未写入调解书的争点；已部分和解、进入履行阶段的
        # 争点留在卷内按调解书执行，不随诉讼移交。
        unresolved = [
            iid for iid in view.issue_order if self._plan_for_issue(view, iid) is None
        ]
        targets = list(issue_ids) if issue_ids is not None else unresolved
        if not targets:
            raise DossierError("没有可移交的未解决争点")
        for issue_id in targets:
            self._require_issue(view, issue_id)
            if self._plan_for_issue(view, issue_id) is not None:
                raise DossierError(
                    f"争点 {issue_id} 已写入调解书，应按调解书履行或另行处理，不能移交诉讼"
                )
        locked_seq = view.seq
        self._append(
            at,
            actor,
            "transferred_to_litigation",
            {
                "issue_ids": targets,
                "handover_note": handover_note,
                "by": by,
                "locked_seq": locked_seq,
            },
        )
        return {
            "issue_ids": tuple(targets),
            "locked_seq": locked_seq,
            "at": at,
            "handover_note": handover_note,
        }

    def _is_resolved(self, view: _View, issue_id: str) -> bool:
        """争点已和解 = 已封存、争议双方全部签收、条款履行完毕。"""

        plan = self._plan_for_issue(view, issue_id)
        if plan is None:
            return False
        for party_id in self._disputing_parties(view):
            signoff = plan.signoffs.get(party_id)
            if signoff is None or not signoff["signed"]:
                return False
        history = plan.performance.get(issue_id, [])
        return bool(history) and history[-1]["status"] == PERFORMANCE_DONE

    def _issue_stage(self, view: _View, issue_id: str, transferred: set[str]) -> str:
        """单个争点的最终阶段（公开查询与结案复核共用）。"""

        if issue_id in transferred:
            return "已移交诉讼"
        if self._is_resolved(view, issue_id):
            return "已和解"
        plan = self._plan_for_issue(view, issue_id)
        if plan is not None:
            history = plan.performance.get(issue_id, [])
            latest = history[-1]["status"] if history else PERFORMANCE_PENDING
            if latest == PERFORMANCE_PARTIAL:
                return "部分和解"
            return "已签收待履行"
        if self._active_consensus(view.issues[issue_id]) is not None:
            return "共识已达成未结"
        return "未达成共识"

    # ----- 查询：分层视图 / 脱敏进度 / 回放复核 -----------------------

    @property
    def events(self) -> tuple[Event, ...]:
        """不可变事件日志（卷宗的唯一事实来源）。"""

        return tuple(self._events)

    @property
    def version(self) -> int:
        """当前卷版本号（事件序号）。"""

        return len(self._events)

    def subjects(self) -> list[dict[str, Any]]:
        """主体层：当事人及其授权变更历史（内部视图，含真实姓名）。"""

        view = self._view()
        return [
            {
                "id": pid,
                "name": view.participants[pid]["name"],
                "role": view.participants[pid]["role"],
                "kind": view.participants[pid]["kind"],
                "is_minor": view.participants[pid]["is_minor"],
                "family_privacy": view.participants[pid]["family_privacy"],
                "authorizations": [
                    dict(authorization)
                    for authorization in view.participants[pid]["authorizations"]
                ],
            }
            for pid in view.participant_order
        ]

    def issues(self) -> list[dict[str, Any]]:
        """争点层：争点及其逐版本共识与撤回记录（内部视图）。"""

        view = self._view()
        result = []
        for issue_id in view.issue_order:
            issue = view.issues[issue_id]
            active = self._active_consensus(issue)
            result.append(
                {
                    "id": issue.id,
                    "type": issue.type,
                    "summary": issue.summary,
                    "opened_at": issue.opened_at,
                    "active_version": None if active is None else active.version,
                    "consensus": [
                        {
                            "version": record.version,
                            "terms": record.terms,
                            "agreed_by": list(record.agreed_by),
                            "at": record.at,
                            "session_id": record.session_id,
                            "basis_evidence_seqs": list(record.basis_evidence_seqs),
                            "withdrawn": None
                            if record.withdrawn is None
                            else dict(record.withdrawn),
                        }
                        for record in issue.consensus
                    ],
                }
            )
        return result

    def evidence_summaries(self) -> list[dict[str, Any]]:
        """证据摘要层：按到达顺序返回（内部视图，含摘要全文）。"""

        return [dict(item) for item in self._view().evidence]

    def statements(self, participant_id: str | None = None) -> list[dict[str, Any]]:
        """陈述层：返回全部（或指定主体的）陈述版本，旧版本一并保留。"""

        view = self._view()
        return [
            dict(item)
            for item in view.statements
            if participant_id is None or item["participant_id"] == participant_id
        ]

    def plans(self) -> list[dict[str, Any]]:
        """调解方案层：封存的方案、签收状态与履行记录（内部视图）。"""

        view = self._view()
        return [
            {
                "id": plan.id,
                "sealed_at": plan.sealed_at,
                "issue_versions": dict(plan.issue_versions),
                "signoffs": {pid: dict(signoff) for pid, signoff in plan.signoffs.items()},
                "performance": {
                    iid: [dict(item) for item in history]
                    for iid, history in plan.performance.items()
                },
            }
            for plan in view.plans.values()
        ]

    def overall_status(self) -> str:
        """卷宗整体状态。"""

        view = self._view()
        if view.transfer is not None:
            return "已移交诉讼"
        resolved = [iid for iid in view.issue_order if self._is_resolved(view, iid)]
        if resolved and len(resolved) == len(view.issue_order):
            return "全部和解"
        if resolved:
            return "部分和解"
        if view.sessions or any(view.issues[i].consensus for i in view.issue_order):
            return "调解中"
        return "已受理"

    def public_progress(self) -> dict[str, Any]:
        """对外公开查询：仅返回脱敏进度，不含任何实体内容。

        - 不返回姓名：自然人以“当事人N”指代，未成年人只显示
          “未成年人N”，不透露姓氏与家庭关系；
        - 不返回争点摘要、证据摘要、陈述、方案条款；
        - 只暴露争点类型、进度状态与计数。
        """

        view = self._view()
        minor_index = 0
        party_index = 0
        public_participants = []
        for pid in view.participant_order:
            participant = view.participants[pid]
            if participant["kind"] == "个人" or participant["is_minor"]:
                if participant["is_minor"]:
                    minor_index += 1
                    code = f"未成年人{minor_index}"
                else:
                    party_index += 1
                    code = f"当事人{party_index}"
            else:
                party_index += 1
                code = f"单位{party_index}"
            public_participants.append({"code": code, "role": participant["role"]})

        transferred = set() if view.transfer is None else view.transfer["issue_ids"]
        public_issues = []
        for index, issue_id in enumerate(view.issue_order, start=1):
            public_issues.append(
                {
                    "code": f"争点{index}",
                    "type": view.issues[issue_id].type,
                    "status": self._issue_stage(view, issue_id, transferred),
                }
            )

        signed_count = sum(
            1
            for plan in view.plans.values()
            for signoff in plan.signoffs.values()
            if signoff["signed"]
        )
        return {
            "dossier_id": view.dossier_id,
            "accepted_at": view.accepted_at.isoformat() if view.accepted_at else None,
            "status": self.overall_status(),
            "participants": public_participants,
            "issues": public_issues,
            "evidence_count": len(view.evidence),
            "late_evidence_count": sum(1 for item in view.evidence if item["late"]),
            "session_count": len(view.sessions),
            "signed_party_count": signed_count,
            "version": view.seq,
        }

    def timeline(self) -> list[dict[str, str]]:
        """按时间顺序回放关键动作（结案复核使用，内部视图）。"""

        view = self._view()
        descriptions = {
            "dossier_accepted": lambda e: (
                "受理立案",
                f"受理时间 {e.at:%Y-%m-%d %H:%M}，举证期限 "
                f"{view.evidence_deadline:%Y-%m-%d}"
                if view.evidence_deadline
                else f"受理时间 {e.at:%Y-%m-%d %H:%M}",
            ),
            "participant_added": lambda e: (
                "登记主体",
                f"{e.data['name']}（{e.data['role']}）"
                + ("，未成年人" if e.data["is_minor"] else "")
                + ("，涉家庭隐私" if e.data["family_privacy"] else ""),
            ),
            "agent_changed": lambda e: (
                "变更代理授权",
                f"{self._name(view, e.data['participant_id'])} 的代理人变更为 "
                f"{e.data['agent_name']}（{e.data.get('basis', '')}）",
            ),
            "agent_revoked": lambda e: (
                "撤销代理授权",
                f"{self._name(view, e.data['participant_id'])} 恢复自行参与",
            ),
            "issue_opened": lambda e: (
                "登记争点",
                f"{e.data['type']}争议：{e.data['summary']}",
            ),
            "evidence_submitted": lambda e: (
                "提交证据（迟到）" if e.data["late"] else "提交证据",
                f"{e.data['title']}，由 {e.data['submitted_by']} 提交",
            ),
            "statement_made": lambda e: (
                "记录陈述",
                f"{self._name(view, e.data['participant_id'])} 陈述"
                + (f"（代理人 {e.data['agent_name']}）" if e.data.get("agent_name") else "")
                + f"：{e.data['content']}",
            ),
            "session_opened": lambda e: (
                "开启调解会话",
                f"调解员 {e.data['mediator']}，地点 {e.data.get('venue', '')}",
            ),
            "session_closed": lambda e: ("结束调解会话", f"会话 {e.data['session_id']}"),
            "consensus_withdrawn": lambda e: (
                f"撤回共识（v{e.data['version']}）",
                f"{e.data['by']} 提出：{e.data['reason']}",
            ),
            "plan_sealed": lambda e: (
                "封存调解书",
                f"方案 {e.data['plan_id']} 锁定争点版本 {e.data['issue_versions']}",
            ),
            "plan_signed": lambda e: (
                "签收调解书" if e.data["signed"] else "拒绝签收",
                f"{self._name(view, e.data['participant_id'])}"
                + (f"（代理人 {e.data['agent_name']}）" if e.data.get("agent_name") else "")
                + (f"，原因：{e.data['reason']}" if not e.data["signed"] else ""),
            ),
            "performance_recorded": lambda e: (
                f"记录履行：{e.data['status']}",
                f"{e.data['by']} 备注：{e.data.get('note', '')}",
            ),
            "transferred_to_litigation": lambda e: (
                "转入诉讼并锁定卷宗",
                f"移交争点 {e.data['issue_ids']}，锁定版本 {e.data['locked_seq']}，"
                f"移交说明：{e.data.get('handover_note', '')}",
            ),
        }
        result = []
        for event in self._events:
            if event.kind == "consensus_reached":
                title = (
                    f"达成共识（{view.issues[event.data['issue_id']].type} "
                    f"v{event.data['version']}）"
                )
                detail = (
                    f"由 {'、'.join(self._name(view, pid) for pid in event.data['agreed_by'])} "
                    f"在会话 {event.data['session_id']} 中同意；依据证据序号 "
                    f"{list(event.data['basis_evidence_seqs'])}；条款：{event.data['terms']}"
                )
            else:
                title, detail = descriptions[event.kind](event)
            result.append(
                {
                    "seq": event.seq,
                    "at": event.at.isoformat(),
                    "actor": event.actor,
                    "title": title,
                    "detail": detail,
                }
            )
        return result

    def closing_review(self) -> dict[str, Any]:
        """结案复核：回放从受理到（部分）和解与诉讼移交的全过程。

        回答三件事：每个争点谁在何时同意了什么（含撤回历史）、
        方案如何签收与履行、未解决部分以哪个锁定版本如何移交。
        """

        view = self._view()
        transferred = set() if view.transfer is None else set(view.transfer["issue_ids"])
        issues_review = []
        for issue_id in view.issue_order:
            issue = view.issues[issue_id]
            plan = self._plan_for_issue(view, issue_id)
            consensus_history = []
            for record in issue.consensus:
                consensus_history.append(
                    {
                        "version": record.version,
                        "at": record.at.isoformat(),
                        "terms": record.terms,
                        "agreed_by": [
                            {"participant_id": pid, "name": self._name(view, pid)}
                            for pid in record.agreed_by
                        ],
                        "session_id": record.session_id,
                        "basis_evidence_seqs": list(record.basis_evidence_seqs),
                        "withdrawn": None
                        if record.withdrawn is None
                        else {
                            "by": record.withdrawn["by"],
                            "reason": record.withdrawn["reason"],
                            "at": record.withdrawn["at"].isoformat(),
                        },
                    }
                )
            if plan is not None:
                signoffs = [
                    {
                        "participant_id": pid,
                        "name": self._name(view, pid),
                        "signed": signoff["signed"],
                        "at": signoff["at"].isoformat(),
                        "agent_name": signoff["agent_name"],
                        "reason": signoff["reason"],
                    }
                    for pid, signoff in plan.signoffs.items()
                ]
                performance = [
                    {
                        "status": item["status"],
                        "by": item["by"],
                        "at": item["at"].isoformat(),
                        "note": item["note"],
                    }
                    for item in plan.performance.get(issue_id, [])
                ]
                sealed_version = plan.issue_versions[issue_id]
            else:
                signoffs = []
                performance = []
                sealed_version = None
            final = self._issue_stage(view, issue_id, transferred)
            issues_review.append(
                {
                    "issue_id": issue_id,
                    "type": issue.type,
                    "opened_at": issue.opened_at.isoformat(),
                    "sealed_consensus_version": sealed_version,
                    "consensus_history": consensus_history,
                    "signoffs": signoffs,
                    "performance": performance,
                    "final": final,
                }
            )

        transfer = None
        if view.transfer is not None:
            transfer = {
                "at": view.transfer["at"].isoformat(),
                "by": view.transfer["by"],
                "issue_ids": list(view.transfer["issue_ids"]),
                "locked_seq": view.transfer["locked_seq"],
                "handover_note": view.transfer["handover_note"],
            }
        return {
            "dossier_id": view.dossier_id,
            "accepted_at": view.accepted_at.isoformat() if view.accepted_at else None,
            "status": self.overall_status(),
            "issues": issues_review,
            "unresolved_issue_ids": [
                item["issue_id"] for item in issues_review if item["final"] != "已和解"
            ],
            "transfer": transfer,
            "timeline": self.timeline(),
        }
