"""林地争议调解卷核心域。

调解卷按五个层次保存：主体、争点、证据摘要、调解方案、签收状态。
所有写操作都转换为只追加的事件，事件带有序号与哈希链；任何一次
“证据迟到、代理人更换、并行调解、部分履行、移交诉讼”都能锁定到
当时的卷宗版本，并可在结案复核时按版本回放。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

# ---------------------------------------------------------------------------
# 常量与错误
# ---------------------------------------------------------------------------

ROLE_MEDIATOR = "mediator"
ORG_JUDICIAL_OFFICE = "judicial_office"  # 司法所
ORG_FORESTRY_STATION = "forestry_station"  # 林业站

PARTY_APPLICANT = "applicant"  # 申请人
PARTY_RESPONDENT = "respondent"  # 被申请人
PARTY_THIRD = "third_party"  # 第三人

ISSUE_BOUNDARY = "boundary"  # 边界
ISSUE_RENT = "rent"  # 租金
ISSUE_ORAL_AGREEMENT = "oral_agreement"  # 历史口头约定
ISSUE_OTHER = "other"

PERFORMANCE_PARTIAL = "partial"  # 部分履行
PERFORMANCE_COMPLETE = "complete"  # 履行完毕

SIGN_SIGNED = "signed"  # 签收
SIGN_REFUSED = "refused"  # 拒签
SIGN_DEEMED = "deemed"  # 留置送达

STATUS_ACCEPTED = "accepted"
STATUS_MEDIATING = "mediating"
STATUS_PARTIAL = "partial_settled"
STATUS_TRANSFERRED = "litigation_transferred"
STATUS_CLOSED = "closed"

MEDIATOR_ORGS = {ORG_JUDICIAL_OFFICE, ORG_FORESTRY_STATION}


class MediationError(Exception):
    """领域规则被违反。"""

    http_status = 400


class NotFoundError(MediationError):
    """卷宗或实体不存在。"""

    http_status = 404


class ConflictError(MediationError):
    """当前版本不允许该操作（并行冲突、卷宗锁定等）。"""

    http_status = 409


class PermissionError(MediationError):  # noqa: A001 - 领域内有意遮蔽内建名
    """操作者无权写入调解卷。"""

    http_status = 403


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """操作调解卷的人（司法所/林业站调解员）。"""

    id: str
    name: str
    role: str
    org: str | None = None


@dataclass(frozen=True)
class Event:
    """不可变事件。seq 为卷宗内版本号，hash 链接前一事件。"""

    seq: int
    at: str
    actor_id: str
    type: str
    data: dict
    prev_hash: str
    hash: str


@dataclass
class AgentAuth:
    """代理人授权（授权变化只追加，不覆盖）。"""

    agent_id: str
    party_id: str
    name: str
    scope: str
    valid_from: str | None
    valid_to: str | None
    document_id: str | None
    active: bool = True


@dataclass
class Party:
    party_id: str
    role: str
    name: str
    id_number: str | None
    contact: str | None
    is_org: bool
    minor: bool
    guardian_party_id: str | None
    privacy_tags: list[str]


@dataclass
class ConsensusRecord:
    """单个争点的一次达成/撤回共识记录。"""

    at: str
    actor_id: str
    plan_id: str
    plan_version: int
    consenters: list[str]
    withdrawn_at: str | None = None
    withdrawn_by: str | None = None
    withdraw_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None


@dataclass
class Issue:
    issue_id: str
    kind: str
    title: str
    description: str
    opened_at: str
    evidence_deadline: str | None
    private: bool
    consensus: list[ConsensusRecord] = field(default_factory=list)

    def active_consensus(self) -> ConsensusRecord | None:
        for record in reversed(self.consensus):
            if record.active:
                return record
        return None


@dataclass
class Statement:
    """当事人或代理人的陈述；补充材料永远新增，不覆盖旧陈述。"""

    statement_id: str
    version: int
    at: str
    maker_id: str
    issue_id: str | None
    content: str
    materials: list[str]
    supersedes: str | None
    privacy_tags: list[str]


@dataclass
class Evidence:
    evidence_id: str
    version: int
    at: str
    submitter_id: str
    issue_id: str
    summary: str
    kind: str
    received_at: str
    late: bool
    private: bool


@dataclass
class Session:
    """调解会议；并行会议之间争点集合不得相交。"""

    session_id: str
    started_at: str
    concluded_at: str | None
    issue_ids: list[str]
    participant_ids: list[str]
    note: str | None


@dataclass
class PlanVersion:
    plan_id: str
    version: int
    at: str
    actor_id: str
    terms: dict[str, str]
    basis_version: int
    basis_evidence_ids: list[str]
    note: str


@dataclass
class Signoff:
    plan_id: str
    plan_version: int
    party_id: str
    status: str
    at: str
    actor_id: str


@dataclass
class Performance:
    issue_id: str
    status: str
    progress: int | None
    note: str
    at: str
    actor_id: str


@dataclass
class CaseView:
    """重放事件得到的卷宗快照。"""

    case_id: str
    version: int
    title: str = ""
    category: str = ISSUE_BOUNDARY
    summary: str = ""
    organizations: list[str] = field(default_factory=list)
    location: dict = field(default_factory=dict)
    status: str = ""
    accepted_at: str | None = None
    closed_at: str | None = None
    close_note: str | None = None
    parties: dict[str, Party] = field(default_factory=dict)
    agents: dict[str, AgentAuth] = field(default_factory=dict)
    issues: dict[str, Issue] = field(default_factory=dict)
    statements: list[Statement] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    plans: list[PlanVersion] = field(default_factory=list)
    signoffs: list[Signoff] = field(default_factory=list)
    performance: list[Performance] = field(default_factory=list)
    transfer: dict | None = None
    events: list[Event] = field(default_factory=list)

    # -- 派生查询 --------------------------------------------------------

    def is_locked(self) -> bool:
        """移交诉讼或结案后，卷宗内容锁定。"""

        return self.status in {STATUS_TRANSFERRED, STATUS_CLOSED}

    def maker_name(self, maker_id: str) -> str:
        if maker_id in self.parties:
            return self.parties[maker_id].name
        if maker_id in self.agents:
            agent = self.agents[maker_id]
            principal = self.parties.get(agent.party_id)
            principal_name = principal.name if principal else agent.party_id
            return f"{agent.name}（代理 {principal_name}）"
        return maker_id

    def plan(self, plan_id: str, version: int) -> PlanVersion | None:
        for plan in self.plans:
            if plan.plan_id == plan_id and plan.version == version:
                return plan
        return None

    def resolved_issues(self) -> list[Issue]:
        return [issue for issue in self.issues.values() if issue.active_consensus()]

    def unresolved_issues(self) -> list[Issue]:
        return [issue for issue in self.issues.values() if not issue.active_consensus()]

    def open_sessions(self) -> list[Session]:
        return [session for session in self.sessions if session.concluded_at is None]

    def current_performance(self, issue_id: str) -> Performance | None:
        for item in reversed(self.performance):
            if item.issue_id == issue_id:
                return item
        return None


# ---------------------------------------------------------------------------
# 事件重放
# ---------------------------------------------------------------------------


def _apply(view: CaseView, event: Event) -> None:
    data = event.data
    kind = event.type

    if kind == "case_accepted":
        view.status = STATUS_ACCEPTED
        view.accepted_at = event.at
        view.title = data["title"]
        view.category = data["category"]
        view.summary = data.get("summary", "")
        view.organizations = list(data.get("organizations", []))
        view.location = dict(data.get("location") or {})
    elif kind == "party_registered":
        view.parties[data["party_id"]] = Party(
            party_id=data["party_id"],
            role=data["role"],
            name=data["name"],
            id_number=data.get("id_number"),
            contact=data.get("contact"),
            is_org=data.get("is_org", False),
            minor=data.get("minor", False),
            guardian_party_id=data.get("guardian_party_id"),
            privacy_tags=list(data.get("privacy_tags", [])),
        )
    elif kind == "agent_authorized":
        view.agents[data["agent_id"]] = AgentAuth(
            agent_id=data["agent_id"],
            party_id=data["party_id"],
            name=data["name"],
            scope=data["scope"],
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            document_id=data.get("document_id"),
        )
    elif kind == "agent_revoked":
        if data["agent_id"] in view.agents:
            view.agents[data["agent_id"]].active = False
    elif kind == "issue_opened":
        view.issues[data["issue_id"]] = Issue(
            issue_id=data["issue_id"],
            kind=data["kind"],
            title=data["title"],
            description=data.get("description", ""),
            opened_at=event.at,
            evidence_deadline=data.get("evidence_deadline"),
            private=data.get("private", False),
        )
    elif kind == "statement_recorded":
        view.statements.append(
            Statement(
                statement_id=data["statement_id"],
                version=event.seq,
                at=event.at,
                maker_id=data["maker_id"],
                issue_id=data.get("issue_id"),
                content=data["content"],
                materials=list(data.get("materials", [])),
                supersedes=data.get("supersedes"),
                privacy_tags=list(data.get("privacy_tags", [])),
            )
        )
    elif kind == "evidence_submitted":
        view.evidence.append(
            Evidence(
                evidence_id=data["evidence_id"],
                version=event.seq,
                at=event.at,
                submitter_id=data["submitter_id"],
                issue_id=data["issue_id"],
                summary=data["summary"],
                kind=data.get("kind", "document"),
                received_at=data["received_at"],
                late=data.get("late", False),
                private=data.get("private", False),
            )
        )
    elif kind == "session_started":
        view.sessions.append(
            Session(
                session_id=data["session_id"],
                started_at=event.at,
                concluded_at=None,
                issue_ids=list(data["issue_ids"]),
                participant_ids=list(data.get("participant_ids", [])),
                note=None,
            )
        )
        if view.status == STATUS_ACCEPTED:
            view.status = STATUS_MEDIATING
    elif kind == "session_concluded":
        for session in view.sessions:
            if session.session_id == data["session_id"]:
                session.concluded_at = event.at
                session.note = data.get("note")
    elif kind == "plan_proposed":
        view.plans.append(
            PlanVersion(
                plan_id=data["plan_id"],
                version=data["version"],
                at=event.at,
                actor_id=event.actor_id,
                terms=dict(data["terms"]),
                basis_version=data["basis_version"],
                basis_evidence_ids=list(data.get("basis_evidence_ids", [])),
                note=data.get("note", ""),
            )
        )
    elif kind == "consensus_reached":
        view.issues[data["issue_id"]].consensus.append(
            ConsensusRecord(
                at=event.at,
                actor_id=event.actor_id,
                plan_id=data["plan_id"],
                plan_version=data["version"],
                consenters=list(data["consenters"]),
            )
        )
        view.status = STATUS_PARTIAL
    elif kind == "consensus_withdrawn":
        issue = view.issues[data["issue_id"]]
        for record in reversed(issue.consensus):
            if record.active:
                record.withdrawn_at = event.at
                record.withdrawn_by = data.get("by_party_id")
                record.withdraw_reason = data.get("reason", "")
                break
    elif kind == "plan_signed":
        view.signoffs.append(
            Signoff(
                plan_id=data["plan_id"],
                plan_version=data["version"],
                party_id=data["party_id"],
                status=data["status"],
                at=event.at,
                actor_id=event.actor_id,
            )
        )
    elif kind == "performance_recorded":
        view.performance.append(
            Performance(
                issue_id=data["issue_id"],
                status=data["status"],
                progress=data.get("progress"),
                note=data.get("note", ""),
                at=event.at,
                actor_id=event.actor_id,
            )
        )
    elif kind == "litigation_transferred":
        view.status = STATUS_TRANSFERRED
        view.transfer = dict(data)
        view.transfer["at"] = event.at
    elif kind == "case_closed":
        view.status = STATUS_CLOSED
        view.closed_at = event.at
        view.close_note = data.get("note", "")


def replay(events: list[Event], upto: int | None = None) -> CaseView:
    """按顺序重放事件得到卷宗快照；upto 可锁定到历史版本。"""

    selected = events if upto is None else events[:upto]
    case_id = selected[0].data["case_id"] if selected else ""
    view = CaseView(case_id=case_id, version=len(selected))
    for event in selected:
        _apply(view, event)
    view.events = list(selected)
    return view


def verify_hash_chain(events: list[Event]) -> bool:
    """校验事件哈希链，发现卷宗被事后篡改时返回 False。"""

    previous = "0" * 64
    for event in events:
        if event.prev_hash != previous:
            return False
        if event.hash != _event_hash(
            event.seq, event.at, event.actor_id, event.type, event.data, event.prev_hash
        ):
            return False
        previous = event.hash
    return True


def _event_hash(
    seq: int, at: str, actor_id: str, event_type: str, data: dict, prev_hash: str
) -> str:
    body = json.dumps(
        [seq, at, actor_id, event_type, data, prev_hash],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------


class MediationService:
    """司法所与林业站共同使用的调解卷写入/查询服务（线程安全）。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._cases: dict[str, list[Event]] = {}
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- 基础 ------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _require_mediator(self, actor: Actor) -> None:
        if actor.role != ROLE_MEDIATOR or actor.org not in MEDIATOR_ORGS:
            raise PermissionError("只有司法所或林业站调解员可以写入调解卷")

    def _events(self, case_id: str) -> list[Event]:
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise NotFoundError(f"调解卷不存在：{case_id}") from exc

    def _view(self, case_id: str) -> CaseView:
        return replay(self._cases[case_id])

    def _append(
        self, events: list[Event], actor_id: str, event_type: str, data: dict
    ) -> Event:
        seq = len(events) + 1
        at = self._now().isoformat()
        prev_hash = events[-1].hash if events else "0" * 64
        digest = _event_hash(seq, at, actor_id, event_type, data, prev_hash)
        event = Event(
            seq=seq,
            at=at,
            actor_id=actor_id,
            type=event_type,
            data=data,
            prev_hash=prev_hash,
            hash=digest,
        )
        events.append(event)
        return event

    def _next_id(self, view: CaseView, prefix: str, attr: str) -> str:
        collection = getattr(view, attr)
        count = len(collection) + 1
        return f"{prefix}{count}"

    def _require_unlocked(self, view: CaseView) -> None:
        if view.is_locked():
            raise ConflictError(
                f"调解卷已锁定（{view.status}），不得再变更内容：{view.case_id}"
            )

    def _require_issue(self, view: CaseView, issue_id: str) -> Issue:
        try:
            return view.issues[issue_id]
        except KeyError as exc:
            raise NotFoundError(f"争点不存在：{issue_id}") from exc

    def _require_party(self, view: CaseView, party_id: str) -> Party:
        try:
            return view.parties[party_id]
        except KeyError as exc:
            raise NotFoundError(f"当事人不存在：{party_id}") from exc

    def _require_known_maker(self, view: CaseView, maker_id: str) -> None:
        if maker_id not in view.parties and maker_id not in view.agents:
            raise NotFoundError(f"陈述主体未登记：{maker_id}")

    # -- 立案与主体层 ----------------------------------------------------

    def accept_case(
        self,
        actor: Actor,
        *,
        case_id: str | None = None,
        title: str = "",
        category: str = ISSUE_BOUNDARY,
        summary: str = "",
        location: dict | None = None,
        organizations: list[str] | tuple[str, ...] = (
            ORG_JUDICIAL_OFFICE,
            ORG_FORESTRY_STATION,
        ),
    ) -> dict:
        """受理立案，建立调解卷。"""

        self._require_mediator(actor)
        with self._lock:
            if case_id is None:
                year = self._now().year
                case_id = f"C{year}{len(self._cases) + 1:04d}"
            if case_id in self._cases:
                raise ConflictError(f"调解卷编号已存在：{case_id}")
            events: list[Event] = []
            self._cases[case_id] = events
            event = self._append(
                events,
                actor.id,
                "case_accepted",
                {
                    "case_id": case_id,
                    "title": title,
                    "category": category,
                    "summary": summary,
                    "location": location or {},
                    "organizations": list(organizations),
                },
            )
            return {"case_id": case_id, "case_version": event.seq}

    def register_party(
        self,
        actor: Actor,
        case_id: str,
        *,
        role: str,
        name: str,
        party_id: str | None = None,
        id_number: str | None = None,
        contact: str | None = None,
        is_org: bool = False,
        minor: bool = False,
        guardian_party_id: str | None = None,
        privacy_tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        """登记当事人（未成年人需标注，监护人另行登记）。"""

        self._require_mediator(actor)
        if role not in {PARTY_APPLICANT, PARTY_RESPONDENT, PARTY_THIRD}:
            raise MediationError(f"未知当事人角色：{role}")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            if party_id is None:
                party_id = self._next_id(view, "P", "parties")
            if party_id in view.parties:
                raise ConflictError(f"当事人编号已存在：{party_id}")
            if guardian_party_id and guardian_party_id not in view.parties:
                raise MediationError("监护人须先登记为当事人")
            event = self._append(
                events,
                actor.id,
                "party_registered",
                {
                    "party_id": party_id,
                    "role": role,
                    "name": name,
                    "id_number": id_number,
                    "contact": contact,
                    "is_org": is_org,
                    "minor": minor,
                    "guardian_party_id": guardian_party_id,
                    "privacy_tags": list(privacy_tags),
                },
            )
            return {"party_id": party_id, "case_version": event.seq}

    def authorize_agent(
        self,
        actor: Actor,
        case_id: str,
        party_id: str,
        *,
        name: str,
        scope: str = "一般授权",
        valid_from: str | None = None,
        valid_to: str | None = None,
        document_id: str | None = None,
        agent_id: str | None = None,
        replace: bool = True,
    ) -> dict:
        """授权或更换代理人。更换时旧授权标记失效，旧陈述仍归属旧代理人。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            self._require_party(view, party_id)
            if agent_id is None:
                agent_id = f"A{len(view.agents) + 1}"
            if agent_id in view.agents:
                raise ConflictError(f"代理人编号已存在：{agent_id}")
            revoked: list[str] = []
            if replace:
                for existing in view.agents.values():
                    if existing.party_id == party_id and existing.active:
                        self._append(
                            events,
                            actor.id,
                            "agent_revoked",
                            {"agent_id": existing.agent_id, "party_id": party_id},
                        )
                        revoked.append(existing.agent_id)
            event = self._append(
                events,
                actor.id,
                "agent_authorized",
                {
                    "agent_id": agent_id,
                    "party_id": party_id,
                    "name": name,
                    "scope": scope,
                    "valid_from": valid_from or self._now().isoformat(),
                    "valid_to": valid_to,
                    "document_id": document_id,
                },
            )
            return {
                "agent_id": agent_id,
                "revoked_agents": revoked,
                "case_version": event.seq,
            }

    # -- 争点层 ----------------------------------------------------------

    def open_issue(
        self,
        actor: Actor,
        case_id: str,
        *,
        kind: str,
        title: str,
        description: str = "",
        issue_id: str | None = None,
        evidence_deadline: str | None = None,
        private: bool = False,
    ) -> dict:
        """登记争点；可设置举证期限，逾期证据仍会收录但标注迟到版本。"""

        self._require_mediator(actor)
        if kind not in {
            ISSUE_BOUNDARY,
            ISSUE_RENT,
            ISSUE_ORAL_AGREEMENT,
            ISSUE_OTHER,
        }:
            raise MediationError(f"未知争点类型：{kind}")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            if issue_id is None:
                issue_id = self._next_id(view, "I", "issues")
            if issue_id in view.issues:
                raise ConflictError(f"争点编号已存在：{issue_id}")
            event = self._append(
                events,
                actor.id,
                "issue_opened",
                {
                    "issue_id": issue_id,
                    "kind": kind,
                    "title": title,
                    "description": description,
                    "evidence_deadline": evidence_deadline,
                    "private": private,
                },
            )
            return {"issue_id": issue_id, "case_version": event.seq}

    def record_statement(
        self,
        actor: Actor,
        case_id: str,
        maker_id: str,
        content: str,
        *,
        issue_id: str | None = None,
        materials: list[str] | tuple[str, ...] = (),
        supersedes: str | None = None,
        privacy_tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        """记录陈述或补充材料；始终新增版本，不覆盖旧陈述。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            self._require_known_maker(view, maker_id)
            if issue_id is not None:
                self._require_issue(view, issue_id)
            if supersedes and not any(
                item.statement_id == supersedes for item in view.statements
            ):
                raise MediationError(f"被补充的陈述不存在：{supersedes}")
            statement_id = f"S{len(view.statements) + 1}"
            event = self._append(
                events,
                actor.id,
                "statement_recorded",
                {
                    "statement_id": statement_id,
                    "maker_id": maker_id,
                    "issue_id": issue_id,
                    "content": content,
                    "materials": list(materials),
                    "supersedes": supersedes,
                    "privacy_tags": list(privacy_tags),
                },
            )
            return {
                "statement_id": statement_id,
                "case_version": event.seq,
                "statement_version": event.seq,
            }

    def submit_evidence(
        self,
        actor: Actor,
        case_id: str,
        submitter_id: str,
        issue_id: str,
        summary: str,
        *,
        kind: str = "document",
        received_at: str | None = None,
        evidence_id: str | None = None,
        private: bool = False,
    ) -> dict:
        """收录证据摘要。超过举证期限的证据标记为迟到但不丢弃、不覆盖。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            self._require_known_maker(view, submitter_id)
            issue = self._require_issue(view, issue_id)
            if evidence_id and any(
                item.evidence_id == evidence_id for item in view.evidence
            ):
                raise ConflictError(f"证据编号已存在：{evidence_id}")
            evidence_id = evidence_id or f"E{len(view.evidence) + 1}"
            received_dt = _parse_time(received_at) or self._now()
            late = bool(
                issue.evidence_deadline
                and received_dt > _parse_time(issue.evidence_deadline)
            )
            event = self._append(
                events,
                actor.id,
                "evidence_submitted",
                {
                    "evidence_id": evidence_id,
                    "submitter_id": submitter_id,
                    "issue_id": issue_id,
                    "summary": summary,
                    "kind": kind,
                    "received_at": received_dt.isoformat(),
                    "deadline": issue.evidence_deadline,
                    "late": late,
                    "private": private,
                },
            )
            return {
                "evidence_id": evidence_id,
                "late": late,
                "case_version": event.seq,
                "evidence_version": event.seq,
            }

    # -- 并行调解会议 ----------------------------------------------------

    def start_session(
        self,
        actor: Actor,
        case_id: str,
        *,
        issue_ids: list[str] | tuple[str, ...],
        participant_ids: list[str] | tuple[str, ...] = (),
        session_id: str | None = None,
    ) -> dict:
        """开始调解会议。并行会议争点不得相交，避免同一争点双线调解。"""

        self._require_mediator(actor)
        if not issue_ids:
            raise MediationError("调解会议必须至少关联一个争点")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            for issue_id in issue_ids:
                self._require_issue(view, issue_id)
            busy = {
                busy_id
                for session in view.open_sessions()
                for busy_id in session.issue_ids
            }
            overlap = sorted(set(issue_ids) & busy)
            if overlap:
                raise ConflictError(f"争点正在另一并行调解会议中：{overlap}")
            session_id = session_id or f"M{len(view.sessions) + 1}"
            event = self._append(
                events,
                actor.id,
                "session_started",
                {
                    "session_id": session_id,
                    "issue_ids": list(issue_ids),
                    "participant_ids": list(participant_ids),
                },
            )
            return {"session_id": session_id, "case_version": event.seq}

    def conclude_session(
        self, actor: Actor, case_id: str, session_id: str, *, note: str = ""
    ) -> dict:
        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            target = next(
                (item for item in view.sessions if item.session_id == session_id), None
            )
            if target is None:
                raise NotFoundError(f"调解会议不存在：{session_id}")
            if target.concluded_at is not None:
                raise ConflictError(f"调解会议已结束：{session_id}")
            event = self._append(
                events,
                actor.id,
                "session_concluded",
                {"session_id": session_id, "note": note},
            )
            return {"session_id": session_id, "case_version": event.seq}

    # -- 方案层 ----------------------------------------------------------

    def propose_plan(
        self,
        actor: Actor,
        case_id: str,
        terms: dict[str, str],
        *,
        plan_id: str = "PL",
        basis_evidence_ids: list[str] | tuple[str, ...] = (),
        note: str = "",
    ) -> dict:
        """提出调解方案新版本，并锁定所依据的卷宗版本与证据清单。"""

        self._require_mediator(actor)
        if not terms:
            raise MediationError("调解方案必须包含至少一项争点处理意见")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            for issue_id in terms:
                self._require_issue(view, issue_id)
            known_evidence = {item.evidence_id for item in view.evidence}
            unknown = sorted(set(basis_evidence_ids) - known_evidence)
            if unknown:
                raise MediationError(f"方案依据证据不存在：{unknown}")
            version = (
                max(
                    (
                        plan.version
                        for plan in view.plans
                        if plan.plan_id == plan_id
                    ),
                    default=0,
                )
                + 1
            )
            event = self._append(
                events,
                actor.id,
                "plan_proposed",
                {
                    "plan_id": plan_id,
                    "version": version,
                    "terms": dict(terms),
                    "basis_version": view.version,
                    "basis_evidence_ids": list(basis_evidence_ids),
                    "note": note,
                },
            )
            return {
                "plan_id": plan_id,
                "version": version,
                "basis_version": view.version,
                "case_version": event.seq,
            }

    def reach_consensus(
        self,
        actor: Actor,
        case_id: str,
        issue_id: str,
        *,
        plan_id: str = "PL",
        version: int,
        consenters: list[str] | tuple[str, ...],
    ) -> dict:
        """对单个争点达成共识；各争点相互独立，需指定锁定的方案版本。"""

        self._require_mediator(actor)
        if not consenters:
            raise MediationError("达成共识至少需要一名同意当事人")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            issue = self._require_issue(view, issue_id)
            plan = view.plan(plan_id, version)
            if plan is None:
                raise NotFoundError(f"方案版本不存在：{plan_id} v{version}")
            if issue_id not in plan.terms:
                raise MediationError(f"方案 {plan_id} v{version} 未涉及争点 {issue_id}")
            if issue.active_consensus() is not None:
                raise ConflictError("该争点已达成共识，须先撤回再重新达成")
            for party_id in consenters:
                self._require_party(view, party_id)
            event = self._append(
                events,
                actor.id,
                "consensus_reached",
                {
                    "issue_id": issue_id,
                    "plan_id": plan_id,
                    "version": version,
                    "consenters": list(consenters),
                },
            )
            return {"issue_id": issue_id, "case_version": event.seq}

    def withdraw_consensus(
        self,
        actor: Actor,
        case_id: str,
        issue_id: str,
        *,
        by_party_id: str | None = None,
        reason: str = "",
    ) -> dict:
        """撤回单个争点的共识；历史达成记录保留备查。已履行完毕的不得撤回。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            issue = self._require_issue(view, issue_id)
            active = issue.active_consensus()
            if active is None:
                raise ConflictError("该争点当前没有生效中的共识")
            current = view.current_performance(issue_id)
            if current is not None and current.status == PERFORMANCE_COMPLETE:
                raise ConflictError("该争点方案已履行完毕，不能撤回共识")
            if by_party_id is not None:
                self._require_party(view, by_party_id)
            event = self._append(
                events,
                actor.id,
                "consensus_withdrawn",
                {"issue_id": issue_id, "by_party_id": by_party_id, "reason": reason},
            )
            return {"issue_id": issue_id, "case_version": event.seq}

    # -- 签收与履行层 ----------------------------------------------------

    def sign_plan(
        self,
        actor: Actor,
        case_id: str,
        plan_id: str,
        version: int,
        party_id: str,
        *,
        status: str = SIGN_SIGNED,
    ) -> dict:
        """记录当事人对方案某版本的签收状态（签收/拒签/留置送达）。"""

        self._require_mediator(actor)
        if status not in {SIGN_SIGNED, SIGN_REFUSED, SIGN_DEEMED}:
            raise MediationError(f"未知签收状态：{status}")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            if view.plan(plan_id, version) is None:
                raise NotFoundError(f"方案版本不存在：{plan_id} v{version}")
            self._require_party(view, party_id)
            event = self._append(
                events,
                actor.id,
                "plan_signed",
                {
                    "plan_id": plan_id,
                    "version": version,
                    "party_id": party_id,
                    "status": status,
                },
            )
            return {"case_version": event.seq}

    def record_performance(
        self,
        actor: Actor,
        case_id: str,
        issue_id: str,
        status: str,
        *,
        progress: int | None = None,
        note: str = "",
    ) -> dict:
        """记录方案履行情况，支持部分履行；以达成共识为前提。"""

        self._require_mediator(actor)
        if status not in {PERFORMANCE_PARTIAL, PERFORMANCE_COMPLETE}:
            raise MediationError(f"未知履行状态：{status}")
        if progress is not None and not 0 <= progress <= 100:
            raise MediationError("履行进度须在 0 至 100 之间")
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            issue = self._require_issue(view, issue_id)
            if issue.active_consensus() is None:
                raise ConflictError("该争点没有生效共识，不能记录履行")
            event = self._append(
                events,
                actor.id,
                "performance_recorded",
                {
                    "issue_id": issue_id,
                    "status": status,
                    "progress": progress,
                    "note": note,
                },
            )
            return {"issue_id": issue_id, "case_version": event.seq}

    # -- 移交诉讼与结案 --------------------------------------------------

    def transfer_to_litigation(
        self, actor: Actor, case_id: str, *, reason: str = "", handover_note: str = ""
    ) -> dict:
        """将未解决争点移交诉讼，并锁定整卷版本与移交清单。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            self._require_unlocked(view)
            unresolved = view.unresolved_issues()
            if not unresolved:
                raise ConflictError("没有未解决争点，无需移交诉讼")
            unresolved_ids = [issue.issue_id for issue in unresolved]
            evidence_ids = [
                item.evidence_id
                for item in view.evidence
                if item.issue_id in unresolved_ids
            ]
            statement_ids = [
                item.statement_id
                for item in view.statements
                if item.issue_id in unresolved_ids
            ]
            # 先确定锁定序号，再连同序号一起入哈希链（禁止事后回填）。
            locked_version = len(events) + 1
            event = self._append(
                events,
                actor.id,
                "litigation_transferred",
                {
                    "reason": reason,
                    "unresolved_issues": [
                        {"issue_id": issue.issue_id, "kind": issue.kind, "title": issue.title}
                        for issue in unresolved
                    ],
                    "handover_evidence_ids": evidence_ids,
                    "handover_statement_ids": statement_ids,
                    "handover_note": handover_note,
                    "locked_version": locked_version,
                },
            )
            return {
                "locked_version": event.seq,
                "unresolved_issues": unresolved_ids,
                "case_version": event.seq,
            }

    def close_case(self, actor: Actor, case_id: str, *, note: str = "") -> dict:
        """结案。全部和解可直接结案；部分和解须先移交未决争点再结案。"""

        self._require_mediator(actor)
        with self._lock:
            events = self._events(case_id)
            view = replay(events)
            if view.status == STATUS_CLOSED:
                raise ConflictError("调解卷已结案")
            if view.transfer is None and view.unresolved_issues():
                raise ConflictError("仍有未解决争点，应先移交诉讼后再结案")
            if view.transfer is not None:
                outcome = (
                    "partial_settlement_and_litigation"
                    if view.resolved_issues()
                    else "litigation"
                )
            else:
                outcome = "full_settlement"
            event = self._append(
                events,
                actor.id,
                "case_closed",
                {"note": note, "outcome": outcome},
            )
            return {"outcome": outcome, "case_version": event.seq}

    # -- 查询 ------------------------------------------------------------

    def load(self, case_id: str) -> CaseView:
        with self._lock:
            return replay(self._events(case_id))

    def load_at(self, case_id: str, version: int) -> CaseView:
        """读取历史版本快照（结案复核回放用）。"""

        with self._lock:
            events = self._events(case_id)
            if not 1 <= version <= len(events):
                raise NotFoundError(f"卷宗版本不存在：{case_id} v{version}")
            return replay(events, upto=version)

    def list_cases(self) -> list[str]:
        with self._lock:
            return sorted(self._cases)
