"""调解卷 HTTP 接口测试。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service.api import ApiHandler, ApiState


def _make_server() -> tuple[ThreadingHTTPServer, ApiState]:
    state = ApiState()

    class _Handler(ApiHandler):
        pass

    _Handler.state = state
    return ThreadingHTTPServer(("127.0.0.1", 0), _Handler), state


MEDIATOR_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Actor-Id": "U1",
    "X-Actor-Name": "Wang-Mediator",
    "X-Actor-Role": "mediator",
    "X-Actor-Org": "judicial_office",
}

PUBLIC_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.state = _make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method: str, path: str, payload: dict | None = None,
                 headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers=headers or PUBLIC_HEADERS,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def _create_case(self) -> tuple[str, str]:
        status, body = self._request("POST", "/api/cases", {
            "title": "青山林场边界争议",
            "category": "boundary",
            "summary": "包含未成年人信息的敏感摘要",
            "location": {"village": "青山村", "compartment": "林班1", "place": "小地名秘密"},
        }, MEDIATOR_HEADERS)
        self.assertEqual(status, 201)
        return body["case_id"], body["public_ref"]

    def test_full_case_flow_over_http(self) -> None:
        case_id, ref = self._create_case()

        for party in [
            {"role": "applicant", "name": "张大山", "minor": False},
            {"role": "third_party", "name": "张二山", "minor": True,
             "guardian_party_id": "P1", "privacy_tags": ["minor"]},
            {"role": "respondent", "name": "陈广财"},
        ]:
            status, _ = self._request("POST", f"/api/cases/{case_id}/parties",
                                      party, MEDIATOR_HEADERS)
            self.assertEqual(status, 201)

        status, _ = self._request("POST", f"/api/cases/{case_id}/issues",
                                  {"kind": "boundary", "title": "边界四至"},
                                  MEDIATOR_HEADERS)
        self.assertEqual(status, 201)
        status, _ = self._request("POST", f"/api/cases/{case_id}/issues",
                                  {"kind": "rent", "title": "租金标准"},
                                  MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        status, body = self._request("POST", f"/api/cases/{case_id}/evidence",
                                     {"submitter_id": "P1", "issue_id": "I1",
                                      "summary": "林权证扫描件"}, MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        status, plan = self._request("POST", f"/api/cases/{case_id}/plans",
                                     {"terms": {"I1": "以山脊分水线为界"},
                                      "basis_evidence_ids": ["E1"]},
                                     MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        status, _ = self._request("POST", f"/api/cases/{case_id}/issues/I1/consensus",
                                  {"version": plan["version"], "consenters": ["P1", "P3"]},
                                  MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        status, _ = self._request("POST", f"/api/cases/{case_id}/signoffs",
                                  {"version": 1, "party_id": "P1"}, MEDIATOR_HEADERS)
        self.assertEqual(status, 201)
        status, _ = self._request("POST", f"/api/cases/{case_id}/performance",
                                  {"issue_id": "I1", "status": "partial", "progress": 30},
                                  MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        status, transfer = self._request(
            "POST", f"/api/cases/{case_id}/litigation-transfer",
            {"reason": "租金未决"}, MEDIATOR_HEADERS)
        self.assertEqual(status, 201)

        # 内部完整卷宗：身份、坐落、证据全可见
        status, full = self._request("GET", f"/api/cases/{case_id}",
                                     headers=MEDIATOR_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(full["version"], transfer["case_version"])
        self.assertTrue(full["locked"])
        self.assertEqual(full["parties"][1]["name"], "张二山")
        self.assertTrue(full["parties"][1]["minor"])
        self.assertEqual(full["location"]["place"], "小地名秘密")

        # 复核报告
        status, review = self._request(
            "GET", f"/api/cases/{case_id}/review", headers=MEDIATOR_HEADERS)
        self.assertEqual(status, 200)
        self.assertTrue(review["integrity_ok"])
        self.assertEqual(review["agreements"][0]["consenters"], ["张大山", "陈广财"])
        self.assertEqual(review["unresolved_issues"][0]["issue_id"], "I2")

        # 公开进度：只有脱敏信息
        status, public = self._request("GET", f"/public/progress/{ref}")
        self.assertEqual(status, 200)
        text = repr(public)
        for secret in ["张大山", "陈广财", "张二山", "小地名", "林权证", "青山", "minor"]:
            self.assertNotIn(secret, text)
        self.assertEqual(public["resolved_count"], 1)
        self.assertTrue(public["litigation_transferred"])

    def test_unauthorized_cannot_read_full_case(self) -> None:
        case_id, _ = self._create_case()
        status, body = self._request("GET", f"/api/cases/{case_id}",
                                     headers=PUBLIC_HEADERS)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "PermissionError")

    def test_public_ref_is_not_enumerable(self) -> None:
        self._create_case()
        status, body = self._request("GET", "/public/progress/YLD-0000000000")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not_found"})

    def test_unknown_case_returns_404(self) -> None:
        status, body = self._request("GET", "/api/cases/NOPE",
                                     headers=MEDIATOR_HEADERS)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NotFoundError")

    def test_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
