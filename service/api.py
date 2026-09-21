"""调解卷 HTTP 接口。

- 内部接口 `/api/...` 面向司法所、林业站调解员，通过请求头标识身份与
  职责机构（生产环境应替换为统一鉴权），返回完整卷宗。
- 公开接口 `/public/progress/<查询编号>` 只返回脱敏进度。
"""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from service import domain
from service.domain import Actor, MediationService
from service.privacy import assert_public_payload_safe, build_public_progress
from service.review import build_review

_REF_SALT = "林地争议调解卷-公开查询编号"


class ApiState:
    """进程内卷宗库与公开查询编号映射（测试可用同一实例）。"""

    def __init__(self) -> None:
        self.service = MediationService()
        self._refs: dict[str, str] = {}

    def public_ref(self, case_id: str) -> str:
        digest = hashlib.sha256(f"{_REF_SALT}:{case_id}".encode("utf-8")).hexdigest()
        ref = f"YLD-{digest[:10].upper()}"
        self._refs[ref] = case_id
        return ref

    def resolve_ref(self, ref: str) -> str | None:
        return self._refs.get(ref)


def _serialize_party(party) -> dict:
    return {
        "party_id": party.party_id,
        "role": party.role,
        "name": party.name,
        "id_number": party.id_number,
        "contact": party.contact,
        "is_org": party.is_org,
        "minor": party.minor,
        "guardian_party_id": party.guardian_party_id,
        "privacy_tags": party.privacy_tags,
    }


def serialize_case(view: domain.CaseView) -> dict[str, Any]:
    """完整卷宗序列化（仅内部调解员接口使用）。"""

    return {
        "case_id": view.case_id,
        "version": view.version,
        "title": view.title,
        "category": view.category,
        "summary": view.summary,
        "organizations": view.organizations,
        "location": view.location,
        "status": view.status,
        "locked": view.is_locked(),
        "accepted_at": view.accepted_at,
        "closed_at": view.closed_at,
        "parties": [_serialize_party(party) for party in view.parties.values()],
        "agents": [vars(agent) for agent in view.agents.values()],
        "issues": [
            {
                "issue_id": issue.issue_id,
                "kind": issue.kind,
                "title": issue.title,
                "description": issue.description,
                "opened_at": issue.opened_at,
                "evidence_deadline": issue.evidence_deadline,
                "private": issue.private,
                "consensus_state": (
                    {
                        "plan_id": issue.active_consensus().plan_id,
                        "plan_version": issue.active_consensus().plan_version,
                        "agreed_at": issue.active_consensus().at,
                        "consenters": issue.active_consensus().consenters,
                    }
                    if issue.active_consensus()
                    else None
                ),
                "consensus_history_count": len(issue.consensus),
            }
            for issue in view.issues.values()
        ],
        "statements": [vars(item) for item in view.statements],
        "evidence": [vars(item) for item in view.evidence],
        "sessions": [vars(item) for item in view.sessions],
        "plans": [vars(item) for item in view.plans],
        "signoffs": [vars(item) for item in view.signoffs],
        "performance": [vars(item) for item in view.performance],
        "litigation_transfer": view.transfer,
    }


class ApiHandler(BaseHTTPRequestHandler):
    """路由内部写入/查询接口与公开脱敏查询。"""

    state: ApiState  # 由工厂函数注入到子类

    # -- HTTP 基础 -------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise domain.MediationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise domain.MediationError("请求体必须是 JSON 对象")
        return payload

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> Actor:
        return Actor(
            id=self.headers.get("X-Actor-Id", ""),
            name=self.headers.get("X-Actor-Name", ""),
            role=self.headers.get("X-Actor-Role", ""),
            org=self.headers.get("X-Actor-Org"),
        )

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, domain.MediationError):
            self._send(exc.http_status, {"error": type(exc).__name__, "message": str(exc)})
        else:
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            if path.startswith("/public/progress/"):
                ref = path.rsplit("/", 1)[-1]
                self._public_progress(ref)
                return
            if path.startswith("/api/cases/"):
                tail = path[len("/api/cases/") :]
                parts = [part for part in tail.split("/") if part]
                if len(parts) == 1:
                    self._get_case(parts[0], query)
                    return
                if len(parts) == 2 and parts[1] == "review":
                    self._get_review(parts[0], query)
                    return
            self._send(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            payload = self._read_json()
            route = self._match_post(path)
            if route is None:
                self._send(404, {"error": "not_found"})
                return
            route(payload)
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._handle_error(exc)

    def _match_post(self, path: str):
        parts = [part for part in path.split("/") if part]
        # /api/cases
        if parts == ["api", "cases"]:
            return self._post_case
        if len(parts) < 4 or parts[0] != "api" or parts[1] != "cases":
            return None
        case_id = parts[2]
        resource, rest = parts[3], parts[4:]

        simple = {
            "parties": self._post_party,
            "agents": self._post_agent,
            "issues": self._post_issue,
            "statements": self._post_statement,
            "evidence": self._post_evidence,
            "plans": self._post_plan,
            "litigation-transfer": self._post_transfer,
            "close": self._post_close,
        }
        if resource in simple and not rest:
            return lambda payload: simple[resource](case_id, payload)
        if resource == "sessions":
            if not rest:
                return lambda payload: self._post_session_start(case_id, payload)
            if len(rest) == 2 and rest[1] == "conclude":
                return lambda payload: self._post_session_conclude(case_id, rest[0], payload)
        if resource == "issues":
            if len(rest) == 2 and rest[1] == "consensus":
                return lambda payload: self._post_consensus(case_id, rest[0], payload)
        if resource == "signoffs" and not rest:
            return lambda payload: self._post_signoff(case_id, payload)
        if resource == "performance" and not rest:
            return lambda payload: self._post_performance(case_id, payload)
        return None

    def log_message(self, format: str, *args: object) -> None:
        return

    # -- 公开 ------------------------------------------------------------

    def _public_progress(self, ref: str) -> None:
        case_id = self.state.resolve_ref(ref)
        if case_id is None:
            # 查无此号与权限不足返回相同形态，避免编号枚举。
            self._send(404, {"error": "not_found"})
            return
        view = self.state.service.load(case_id)
        payload = build_public_progress(view, ref)
        assert_public_payload_safe(payload)
        self._send(200, payload)

    # -- 内部查询 --------------------------------------------------------

    def _get_case(self, case_id: str, query: dict) -> None:
        actor = self._actor()
        if actor.role != domain.ROLE_MEDIATOR:
            raise domain.PermissionError("只有调解员可以查阅完整调解卷")
        version_text = query.get("version", [None])[0]
        if version_text:
            view = self.state.service.load_at(case_id, int(version_text))
        else:
            view = self.state.service.load(case_id)
        self._send(200, serialize_case(view))

    def _get_review(self, case_id: str, query: dict) -> None:
        actor = self._actor()
        if actor.role != domain.ROLE_MEDIATOR:
            raise domain.PermissionError("只有调解员可以执行结案复核")
        version_text = query.get("version", [None])[0]
        report = build_review(
            self.state.service, case_id, int(version_text) if version_text else None
        )
        self._send(200, report)

    # -- 内部写入 --------------------------------------------------------

    def _post_case(self, payload: dict) -> None:
        result = self.state.service.accept_case(
            self._actor(),
            title=payload.get("title", ""),
            category=payload.get("category", domain.ISSUE_BOUNDARY),
            summary=payload.get("summary", ""),
            location=payload.get("location"),
            organizations=payload.get(
                "organizations",
                [domain.ORG_JUDICIAL_OFFICE, domain.ORG_FORESTRY_STATION],
            ),
        )
        result["public_ref"] = self.state.public_ref(result["case_id"])
        self._send(201, result)

    def _post_party(self, case_id: str, payload: dict) -> None:
        result = self.state.service.register_party(
            self._actor(),
            case_id,
            role=payload["role"],
            name=payload["name"],
            party_id=payload.get("party_id"),
            id_number=payload.get("id_number"),
            contact=payload.get("contact"),
            is_org=payload.get("is_org", False),
            minor=payload.get("minor", False),
            guardian_party_id=payload.get("guardian_party_id"),
            privacy_tags=payload.get("privacy_tags", []),
        )
        self._send(201, result)

    def _post_agent(self, case_id: str, payload: dict) -> None:
        result = self.state.service.authorize_agent(
            self._actor(),
            case_id,
            payload["party_id"],
            name=payload["name"],
            scope=payload.get("scope", "一般授权"),
            valid_from=payload.get("valid_from"),
            valid_to=payload.get("valid_to"),
            document_id=payload.get("document_id"),
            agent_id=payload.get("agent_id"),
            replace=payload.get("replace", True),
        )
        self._send(201, result)

    def _post_issue(self, case_id: str, payload: dict) -> None:
        result = self.state.service.open_issue(
            self._actor(),
            case_id,
            kind=payload["kind"],
            title=payload["title"],
            description=payload.get("description", ""),
            issue_id=payload.get("issue_id"),
            evidence_deadline=payload.get("evidence_deadline"),
            private=payload.get("private", False),
        )
        self._send(201, result)

    def _post_statement(self, case_id: str, payload: dict) -> None:
        result = self.state.service.record_statement(
            self._actor(),
            case_id,
            payload["maker_id"],
            payload["content"],
            issue_id=payload.get("issue_id"),
            materials=payload.get("materials", []),
            supersedes=payload.get("supersedes"),
            privacy_tags=payload.get("privacy_tags", []),
        )
        self._send(201, result)

    def _post_evidence(self, case_id: str, payload: dict) -> None:
        result = self.state.service.submit_evidence(
            self._actor(),
            case_id,
            payload["submitter_id"],
            payload["issue_id"],
            payload["summary"],
            kind=payload.get("kind", "document"),
            received_at=payload.get("received_at"),
            evidence_id=payload.get("evidence_id"),
            private=payload.get("private", False),
        )
        self._send(201, result)

    def _post_session_start(self, case_id: str, payload: dict) -> None:
        result = self.state.service.start_session(
            self._actor(),
            case_id,
            issue_ids=payload["issue_ids"],
            participant_ids=payload.get("participant_ids", []),
            session_id=payload.get("session_id"),
        )
        self._send(201, result)

    def _post_session_conclude(self, case_id: str, session_id: str, payload: dict) -> None:
        result = self.state.service.conclude_session(
            self._actor(),
            case_id,
            session_id,
            note=payload.get("note", ""),
        )
        self._send(200, result)

    def _post_plan(self, case_id: str, payload: dict) -> None:
        result = self.state.service.propose_plan(
            self._actor(),
            case_id,
            payload["terms"],
            plan_id=payload.get("plan_id", "PL"),
            basis_evidence_ids=payload.get("basis_evidence_ids", []),
            note=payload.get("note", ""),
        )
        self._send(201, result)

    def _post_consensus(self, case_id: str, issue_id: str, payload: dict) -> None:
        if payload.get("action") == "withdraw":
            result = self.state.service.withdraw_consensus(
                self._actor(),
                case_id,
                issue_id,
                by_party_id=payload.get("by_party_id"),
                reason=payload.get("reason", ""),
            )
        else:
            result = self.state.service.reach_consensus(
                self._actor(),
                case_id,
                issue_id,
                plan_id=payload.get("plan_id", "PL"),
                version=payload["version"],
                consenters=payload["consenters"],
            )
        self._send(201, result)

    def _post_signoff(self, case_id: str, payload: dict) -> None:
        result = self.state.service.sign_plan(
            self._actor(),
            case_id,
            payload.get("plan_id", "PL"),
            payload["version"],
            payload["party_id"],
            status=payload.get("status", domain.SIGN_SIGNED),
        )
        self._send(201, result)

    def _post_performance(self, case_id: str, payload: dict) -> None:
        result = self.state.service.record_performance(
            self._actor(),
            case_id,
            payload["issue_id"],
            payload["status"],
            progress=payload.get("progress"),
            note=payload.get("note", ""),
        )
        self._send(201, result)

    def _post_transfer(self, case_id: str, payload: dict) -> None:
        result = self.state.service.transfer_to_litigation(
            self._actor(),
            case_id,
            reason=payload.get("reason", ""),
            handover_note=payload.get("handover_note", ""),
        )
        self._send(201, result)

    def _post_close(self, case_id: str, payload: dict) -> None:
        result = self.state.service.close_case(
            self._actor(), case_id, note=payload.get("note", "")
        )
        self._send(200, result)
