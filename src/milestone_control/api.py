"""里程碑与变更控制服务的 HTTP/JSON 边界（仅用标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from science_strategy_foundation.errors import DomainError
from science_strategy_foundation.api import route as foundation_route
from science_strategy_foundation.service import DomainService

from .errors import GateBlocked
from .service import MilestoneControlService
from .storage import MilestoneDatabase

# POST 路径 -> (服务方法, 是否幂等写)
WRITE_ROUTES = {
    "/mc/projects": "register_project",
    "/mc/projects/revise": "revise_project",
    "/mc/topics": "register_topic",
    "/mc/work-packages": "register_work_package",
    "/mc/metrics": "register_metric",
    "/mc/metrics/revise": "revise_metric",
    "/mc/evidence": "register_evidence",
    "/mc/evidence/revise": "revise_evidence",
    "/mc/experts": "register_expert",
    "/mc/experts/revise": "revise_expert",
    "/mc/milestones": "register_milestone",
    "/mc/milestones/revise": "revise_milestone",
    "/mc/dependencies": "register_dependency",
    "/mc/dependencies/revise": "revise_dependency",
    "/mc/budget-installments": "register_budget_installment",
    "/mc/commitments": "register_commitment",
    "/mc/commitments/revise": "revise_commitment",
    "/mc/gates/open": "open_gate_round",
    "/mc/gates/acceptance": "submit_acceptance",
    "/mc/gates/sign": "sign_gate",
    "/mc/gates/signoff/withdraw": "withdraw_signoff",
    "/mc/gates/decide": "decide_gate",
    "/mc/gates/rectification": "issue_rectification",
    "/mc/acceptance/withdraw": "withdraw_acceptance",
    "/mc/topics/terminate": "terminate_topic",
    "/mc/budget/mark-paid": "mark_installment_paid",
    "/mc/budget/close": "close_installment",
}


def route(service: MilestoneControlService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          base_service: DomainService | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")

    # 基础服务路由（机构/操作者/场所/通用资料）与 /health
    if not parsed.path.startswith("/mc/"):
        if base_service is not None:
            return foundation_route(base_service, method, path, body, headers)
        return 404, {"error": "route_not_found", "message": "接口不存在"}

    try:
        if method == "GET" and parsed.path == "/mc/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        if method == "POST" and parsed.path in WRITE_ROUTES:
            method_name = WRITE_ROUTES[parsed.path]
            result = getattr(service, method_name)(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result

        if method == "GET" and parsed.path == "/mc/gates/status":
            query = parse_qs(parsed.query)
            gate = query.get("gate_milestone_id", [""])[0]
            if not gate:
                return 400, {"error": "invalid_request",
                             "message": "gate_milestone_id 不能为空"}
            return 200, service.gate_status(gate)

        if method == "GET" and parsed.path == "/mc/funds/pending":
            query = parse_qs(parsed.query)
            project_id = query.get("project_id", [""])[0]
            return 200, service.pending_funds(project_id)

        if method == "GET" and parsed.path == "/mc/critical-path":
            query = parse_qs(parsed.query)
            project_id = query.get("project_id", [""])[0]
            return 200, service.critical_path(project_id)

        if method == "GET" and parsed.path == "/mc/changes":
            query = parse_qs(parsed.query)
            return 200, service.list_changes(
                origin_type=query.get("origin_type", [None])[0],
                origin_id=query.get("origin_id", [None])[0])

        if method == "GET" and parsed.path.startswith("/mc/changes/"):
            change_id = parsed.path.rsplit("/", 1)[-1]
            return 200, service.change_impact(change_id)

        if method == "GET" and parsed.path.startswith("/mc/versions/"):
            rest = parsed.path[len("/mc/versions/"):]
            entity, sep, entity_id = rest.partition("/")
            if not sep or not entity_id:
                return 400, {"error": "invalid_request",
                             "message": "路径必须为 /mc/versions/<entity>/<id>"}
            return 200, service.list_versions(entity, entity_id)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except GateBlocked as exc:
        return exc.status, {"error": exc.code, "message": str(exc), "reasons": exc.reasons}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    service: MilestoneControlService
    base_service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(
            self.service, self.command, self.path, body,
            {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
            base_service=self.base_service)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="启动项目里程碑与变更控制服务")
    parser.add_argument("--database", default="milestone_control.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database = MilestoneDatabase(args.database)
    Handler.base_service = DomainService(database)
    Handler.service = MilestoneControlService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
