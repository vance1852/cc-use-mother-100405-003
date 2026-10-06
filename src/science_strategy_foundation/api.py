"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .milestone_service import MilestoneService
from .service import DomainService
from .storage import Database


def _dataclass_list(items):
    return [item.__dict__ for item in items]


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = route_milestones(service.milestones, method, parsed, query, body,
                                           actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        result = {"error": exc.code, "message": str(exc)}
        if exc.code == "precondition_failed":
            result["blocks"] = exc.blocks
        return exc.status, result
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def route_milestones(ms: MilestoneService, method: str, parsed, query,
                     body: dict[str, Any], actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """里程碑与变更控制子域路由。"""

    path = parsed.path
    # ---- 版本化主体登记 / 修订 ----
    if method == "POST" and path == "/programs":
        return _receipt(ms.register_program(actor_id=actor_id, **body))
    if method == "POST" and path == "/programs/revise":
        return _receipt(ms.revise_program(actor_id=actor_id, **body))
    if method == "POST" and path == "/projects":
        return _receipt(ms.register_project(actor_id=actor_id, **body))
    if method == "POST" and path == "/work-packages":
        return _receipt(ms.register_work_package(actor_id=actor_id, **body))
    if method == "POST" and path == "/milestones":
        return _receipt(ms.register_milestone(actor_id=actor_id, **body))
    if method == "POST" and path == "/milestones/revise":
        return _receipt(ms.revise_milestone(actor_id=actor_id, **body))
    if method == "POST" and path == "/dependencies":
        return _receipt(ms.register_dependency(actor_id=actor_id, **body))
    if method == "POST" and path == "/dependencies/deprecate":
        return _receipt(ms.deprecate_dependency(actor_id=actor_id, **body))
    if method == "POST" and path == "/calibers":
        return _receipt(ms.register_caliber(actor_id=actor_id, **body))
    if method == "POST" and path == "/calibers/revise":
        return _receipt(ms.revise_caliber(actor_id=actor_id, **body))
    # ---- 专家 / 资格 / 证据 / 结论 ----
    if method == "POST" and path == "/experts":
        return _receipt(ms.register_expert(actor_id=actor_id, **body))
    if method == "POST" and path == "/qualifications":
        return _receipt(ms.add_qualification(actor_id=actor_id, **body))
    if method == "POST" and path == "/qualifications/revoke":
        return _receipt(ms.revoke_qualification(actor_id=actor_id, **body))
    if method == "POST" and path == "/evidence":
        return _receipt(ms.submit_evidence(actor_id=actor_id, **body))
    if method == "POST" and path == "/evidence/void":
        return _receipt(ms.void_evidence(actor_id=actor_id, **body))
    if method == "POST" and path == "/verdicts":
        return _receipt(ms.submit_verdict(actor_id=actor_id, **body))
    if method == "POST" and path == "/verdicts/withdraw":
        return _receipt(ms.withdraw_verdict(actor_id=actor_id, **body))
    # ---- 预算 / 支付 / 承诺 ----
    if method == "POST" and path == "/budget-plans":
        return _receipt(ms.register_budget_plan(actor_id=actor_id, **body))
    if method == "POST" and path == "/budget-plans/revise":
        return _receipt(ms.revise_budget_plan(actor_id=actor_id, **body))
    if method == "POST" and path == "/payments":
        return _receipt(ms.pay_release(actor_id=actor_id, **body))
    if method == "POST" and path == "/payments/close":
        return _receipt(ms.close_payment(actor_id=actor_id, **body))
    if method == "POST" and path == "/commitments":
        return _receipt(ms.register_commitment(actor_id=actor_id, **body))
    # ---- 阶段门 / 整改 / 终止 ----
    if method == "POST" and path == "/gates/decide":
        return _receipt(ms.decide_gate(actor_id=actor_id, **body))
    if method == "POST" and path == "/rectifications":
        return _receipt(ms.open_rectification(actor_id=actor_id, **body))
    if method == "POST" and path == "/projects/terminate":
        return _receipt(ms.terminate_project(actor_id=actor_id, **body))
    # ---- 查询视图 ----
    if method == "GET" and path == "/gates/evaluate":
        milestone_id = _one(query, "milestone_id")
        return 200, _evaluation(ms.evaluate_gate(milestone_id, actor_id or None))
    if method == "GET" and path == "/projects/critical-path":
        return 200, ms.critical_path(_one(query, "project_id"))
    if method == "GET" and path == "/projects/funds":
        return 200, ms.project_funds(_one(query, "project_id"))
    if method == "GET" and path == "/changes/impact":
        return 200, ms.change_impact(_one(query, "decision_id"))
    if method == "GET" and path == "/milestones":
        items = _dataclass_list(ms.list_milestones(_one(query, "project_id")))
        return 200, {"items": items}
    if method == "GET" and path == "/verdicts":
        return 200, {"items": _dataclass_list(ms.list_verdicts(_one(query, "milestone_id")))}
    if method == "GET" and path == "/decisions":
        subject_type = query.get("subject_type", [None])[0]
        subject_id = query.get("subject_id", [None])[0]
        return 200, {"items": _dataclass_list(ms.list_decisions(subject_type, subject_id))}
    if method == "GET" and path.startswith("/decisions/"):
        decision_id = path.rsplit("/", 1)[-1]
        descendants = query.get("with_descendants", ["0"])[0] == "1"
        if descendants:
            return 200, {"decision": ms.get_decision(decision_id).__dict__,
                         "descendants": _dataclass_list(ms.decision_descendants(decision_id))}
        return 200, ms.get_decision(decision_id).__dict__
    if method == "GET" and path == "/commitments":
        subject_type = query.get("subject_type", [None])[0]
        subject_id = query.get("subject_id", [None])[0]
        return 200, {"items": _dataclass_list(ms.list_commitments(subject_type, subject_id))}
    return None, {}


def _one(query, name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def _receipt(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result.get("replayed") else 201), result


def _evaluation(evaluation) -> dict[str, Any]:
    return {
        "milestone_id": evaluation.milestone_id,
        "milestone_version": evaluation.milestone_version,
        "passed": evaluation.passed,
        "partial": evaluation.partial,
        "blocks": [block.__dict__ for block in evaluation.blocks],
        "satisfied_prerequisites": list(evaluation.satisfied_prerequisites),
        "accepted_verdicts": [v.__dict__ for v in evaluation.accepted_verdicts],
        "releaseable_amount": evaluation.releaseable_amount,
        "installment_id": evaluation.installment_id}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
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
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动科技战略协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
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
