"""项目里程碑与变更控制服务。

在基础服务的权限、幂等、SQLite 事务与哈希审计链之上，提供：

- 专项/课题/工作包/里程碑/依赖/指标口径/预算分期/交付承诺的不可变版本化；
- 证据按指标口径兼容类唯一占用，防止跨不兼容口径或跨门重复使用；
- 阶段门在单事务内原子完成「前置里程碑有效 + 独立验收有效」判定与额度释放；
- 路线调整、部分通过、限期整改、结论撤回、课题终止沿依赖闭包生成确定性后继决定；
- 已支付/已关账款项与旧版结论全程保留，支持审计与回放；
- 关键路径、资金阻塞原因与变化影响面查询。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    PreconditionError,
    StateError,
    ValidationError,
)
from .graph import CycleError, critical_path, detect_cycle, transitive_closure
from .models_milestone import (
    CaliberView,
    CommitmentView,
    CriticalPathNode,
    DecisionView,
    DependencyView,
    GateBlock,
    GateEvaluation,
    ImpactItem,
    InstallmentView,
    MilestoneView,
    VerdictView,
)
from .storage import Database

NAMESPACE = uuid.UUID("6f3a2c91-7b4e-4d8a-9c21-1e5f0a8d3b64")

WRITE_ROLES = ("admin", "operator")
EVIDENCE_ROLES = ("admin", "operator", "reviewer")

MILESTONE_GATE_KINDS = ("gate_pass", "gate_partial")


def _decision_id(*parts: str) -> str:
    return uuid.uuid5(NAMESPACE, "|".join(parts)).hex


class MilestoneService:
    """协调版本化、阶段门、占用控制、变更级联与审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 64 or not all(c.isalnum() or c in "_.:-" for c in value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _amount(self, value: Any, field: str = "amount") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数（最小货币单位）")
        return value

    def _actor(self, conn, actor_id: str):
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            import json
            response = json.loads(row["response_json"])
            return {**response, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _check_replay(self, conn, *, request_id: str, action: str, payload: dict[str, Any]
                      ) -> dict[str, Any] | None:
        """若请求编号已存在则返回回放结果，否则返回 None（在状态校验之前调用）。"""
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        import json
        response = json.loads(row["response_json"])
        return {**response, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True}

    def _store_receipt(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                       resource_type: str, resource_id: str, response: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (self._identifier(request_id, "request_id"), action, digest(payload),
             resource_type, resource_id, canonical_json(response), self._now()))

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------ 版本化主体

    def _bump_version(self, conn, table: str, id_column: str, object_id: str) -> int:
        row = conn.execute(f"SELECT current_version FROM {table} WHERE {id_column}=?",
                           (object_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{table} 不存在")
        return int(row["current_version"]) + 1

    def register_program(self, *, request_id: str, actor_id: str, program_id: str,
                         name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "program_id": program_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            program_id = self._identifier(program_id, "program_id")
            name = self._text(name, "name")
            now = self._now()

            def create():
                try:
                    conn.execute("INSERT INTO programs(program_id,current_version,created_at) VALUES(?,1,?)",
                                 (program_id, now))
                    conn.execute(
                        "INSERT INTO program_versions(program_id,version,name,status,created_by,created_at) "
                        "VALUES(?,1,?,'effective',?,?)",
                        (program_id, name, actor_id, now))
                except Exception as exc:
                    raise ConflictError("专项编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="program.registered",
                            resource_type="program", resource_id=program_id,
                            detail={"name": name, "version": 1})
                return "program", program_id, {"program_id": program_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="register_program",
                                    payload=payload, create=create)

    def revise_program(self, *, request_id: str, actor_id: str, program_id: str,
                       name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "program_id": program_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            program_id = self._identifier(program_id, "program_id")
            name = self._text(name, "name")
            now = self._now()

            def create():
                current = conn.execute("SELECT current_version FROM programs WHERE program_id=?",
                                       (program_id,)).fetchone()
                if current is None:
                    raise NotFoundError("专项不存在")
                new_version = int(current["current_version"]) + 1
                conn.execute("UPDATE program_versions SET status='superseded' WHERE program_id=? AND status='effective'",
                             (program_id,))
                conn.execute("UPDATE programs SET current_version=? WHERE program_id=?",
                             (new_version, program_id))
                conn.execute(
                    "INSERT INTO program_versions(program_id,version,name,status,created_by,created_at,request_id) "
                    "VALUES(?,?,?,'effective',?,?,?)",
                    (program_id, new_version, name, actor_id, now, request_id))
                self._audit(conn, actor_id=actor_id, action="program.revised",
                            resource_type="program", resource_id=program_id,
                            detail={"version": new_version, "name": name})
                return "program", program_id, {"program_id": program_id, "version": new_version}

            return self._idempotent(conn, request_id=request_id, action="revise_program",
                                    payload=payload, create=create)

    def register_project(self, *, request_id: str, actor_id: str, project_id: str,
                         program_id: str, name: str, undertaking_org_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "project_id": project_id, "program_id": program_id,
                   "name": name, "undertaking_org_id": undertaking_org_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            project_id = self._identifier(project_id, "project_id")
            program_id = self._identifier(program_id, "program_id")
            name = self._text(name, "name")
            undertaking_org_id = self._identifier(undertaking_org_id, "undertaking_org_id")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (undertaking_org_id,)).fetchone() is None:
                raise NotFoundError("承担机构不存在")
            if conn.execute("SELECT 1 FROM programs WHERE program_id=?", (program_id,)).fetchone() is None:
                raise NotFoundError("专项不存在")
            now = self._now()

            def create():
                try:
                    conn.execute("INSERT INTO projects(project_id,program_id,current_version,created_at) VALUES(?, ?,1,?)",
                                 (project_id, program_id, now))
                    conn.execute(
                        "INSERT INTO project_versions(project_id,version,name,undertaking_org_id,status,"
                        "created_by,created_at) VALUES(?,1,?,?,'effective',?,?)",
                        (project_id, name, undertaking_org_id, actor_id, now))
                except Exception as exc:
                    raise ConflictError("课题编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="project.registered",
                            resource_type="project", resource_id=project_id,
                            detail={"program_id": program_id, "name": name,
                                    "undertaking_org_id": undertaking_org_id})
                return "project", project_id, {"project_id": project_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="register_project",
                                    payload=payload, create=create)

    def register_work_package(self, *, request_id: str, actor_id: str, wp_id: str,
                              project_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "wp_id": wp_id, "project_id": project_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            wp_id = self._identifier(wp_id, "wp_id")
            project_id = self._identifier(project_id, "project_id")
            name = self._text(name, "name")
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise NotFoundError("课题不存在")
            now = self._now()

            def create():
                try:
                    conn.execute("INSERT INTO work_packages(wp_id,project_id,current_version,created_at) VALUES(?, ?,1,?)",
                                 (wp_id, project_id, now))
                    conn.execute(
                        "INSERT INTO wp_versions(wp_id,version,name,status,created_by,created_at) "
                        "VALUES(?,1,?,'effective',?,?)",
                        (wp_id, name, actor_id, now))
                except Exception as exc:
                    raise ConflictError("工作包编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="work_package.registered",
                            resource_type="work_package", resource_id=wp_id,
                            detail={"project_id": project_id, "name": name})
                return "work_package", wp_id, {"wp_id": wp_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="register_work_package",
                                    payload=payload, create=create)

    def register_milestone(self, *, request_id: str, actor_id: str, milestone_id: str, wp_id: str,
                           name: str, seq_no: int, planned_days: int,
                           requirements: list[dict[str, Any]],
                           planned_date: str | None = None) -> dict[str, Any]:
        """登记里程碑。requirements: [{caliber_id, required_verdicts}]，绑定口径当前版本。"""
        payload = {"actor_id": actor_id, "milestone_id": milestone_id, "wp_id": wp_id,
                   "name": name, "seq_no": seq_no, "planned_days": planned_days,
                   "requirements": requirements, "planned_date": planned_date}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            milestone_id = self._identifier(milestone_id, "milestone_id")
            wp_id = self._identifier(wp_id, "wp_id")
            name = self._text(name, "name")
            if not isinstance(seq_no, int) or isinstance(seq_no, bool) or seq_no < 0:
                raise ValidationError("seq_no 必须是非负整数")
            if not isinstance(planned_days, int) or isinstance(planned_days, bool) or planned_days < 0:
                raise ValidationError("planned_days 必须是非负整数")
            wp = conn.execute("SELECT * FROM work_packages WHERE wp_id=?", (wp_id,)).fetchone()
            if wp is None:
                raise NotFoundError("工作包不存在")
            bound = self._bind_requirements(conn, requirements)
            now = self._now()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO milestones(milestone_id,wp_id,current_version,created_at) VALUES(?, ?,1,?)",
                        (milestone_id, wp_id, now))
                    conn.execute(
                        "INSERT INTO milestone_versions(milestone_id,version,name,seq_no,planned_days,"
                        "planned_date,status,created_by,created_at) VALUES(?,1,?,?,?,?,'effective',?,?)",
                        (milestone_id, name, seq_no, planned_days, planned_date, actor_id, now))
                except Exception as exc:
                    raise ConflictError("里程碑编号已经存在") from exc
                for caliber_id, caliber_version, required in bound:
                    conn.execute(
                        "INSERT INTO milestone_requirements(milestone_id,version,caliber_id,"
                        "caliber_version,required_verdicts) VALUES(?,?,?,?,?)",
                        (milestone_id, 1, caliber_id, caliber_version, required))
                self._audit(conn, actor_id=actor_id, action="milestone.registered",
                            resource_type="milestone", resource_id=milestone_id,
                            detail={"wp_id": wp_id, "name": name, "seq_no": seq_no,
                                    "planned_days": planned_days,
                                    "requirements": [
                                        {"caliber_id": c, "caliber_version": v, "required_verdicts": r}
                                        for c, v, r in bound]})
                return "milestone", milestone_id, {"milestone_id": milestone_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="register_milestone",
                                    payload=payload, create=create)

    def _bind_requirements(self, conn, requirements: list[dict[str, Any]]
                           ) -> list[tuple[str, int, int]]:
        if not isinstance(requirements, list) or not requirements:
            raise ValidationError("requirements 必须是非空数组")
        bound: list[tuple[str, int, int]] = []
        seen: set[str] = set()
        for item in requirements:
            if not isinstance(item, dict):
                raise ValidationError("requirements 条目必须是对象")
            caliber_id = self._identifier(item.get("caliber_id", ""), "caliber_id")
            required = item.get("required_verdicts", 1)
            if not isinstance(required, int) or isinstance(required, bool) or required < 1:
                raise ValidationError("required_verdicts 必须是正整数")
            if caliber_id in seen:
                raise ValidationError(f"口径 {caliber_id} 在同一里程碑中重复")
            seen.add(caliber_id)
            current = conn.execute(
                "SELECT current_version FROM metric_calibers WHERE caliber_id=?", (caliber_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError(f"口径 {caliber_id} 不存在")
            caliber_version = int(current["current_version"])
            bound.append((caliber_id, caliber_version, required))
        return sorted(bound, key=lambda x: x[0])

    def revise_milestone(self, *, request_id: str, actor_id: str, milestone_id: str, name: str,
                         seq_no: int, planned_days: int,
                         requirements: list[dict[str, Any]], planned_date: str | None = None,
                         reason: str = "") -> dict[str, Any]:
        """修订里程碑（技术路线调整），旧版本及其结论/放款保留可审计，沿依赖传播影响。"""
        payload = {"actor_id": actor_id, "milestone_id": milestone_id, "name": name,
                   "seq_no": seq_no, "planned_days": planned_days,
                   "requirements": requirements, "planned_date": planned_date, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._check_replay(conn, request_id=request_id, action="revise_milestone",
                                        payload=payload)
            if replay is not None:
                return replay
            milestone_id = self._identifier(milestone_id, "milestone_id")
            name = self._text(name, "name")
            if not isinstance(seq_no, int) or isinstance(seq_no, bool) or seq_no < 0:
                raise ValidationError("seq_no 必须是非负整数")
            if not isinstance(planned_days, int) or isinstance(planned_days, bool) or planned_days < 0:
                raise ValidationError("planned_days 必须是非负整数")
            bound = self._bind_requirements(conn, requirements)
            reason = self._text(reason or "路线调整", "reason")
            current = self._load_milestone_current(conn, milestone_id)
            if current["status"] == "terminated":
                raise StateError("里程碑已随课题终止，不能修订")
            new_version = int(current["version"]) + 1
            now = self._now()
            conn.execute(
                "UPDATE milestone_versions SET status='superseded' WHERE milestone_id=? AND status='effective'",
                (milestone_id,))
            conn.execute("UPDATE milestones SET current_version=? WHERE milestone_id=?",
                         (new_version, milestone_id))
            conn.execute(
                "INSERT INTO milestone_versions(milestone_id,version,name,seq_no,planned_days,planned_date,"
                "status,created_by,created_at,request_id) VALUES(?,?,?,?,?,?,'effective',?,?,?)",
                (milestone_id, new_version, name, seq_no, planned_days, planned_date,
                 actor_id, now, request_id))
            for caliber_id, caliber_version, required in bound:
                conn.execute(
                    "INSERT INTO milestone_requirements(milestone_id,version,caliber_id,caliber_version,"
                    "required_verdicts) VALUES(?,?,?,?,?)",
                    (milestone_id, new_version, caliber_id, caliber_version, required))
            superseded_decisions = self._supersede_gate_decisions(
                conn, milestone_id=milestone_id, milestone_version=int(current["version"]))
            self._free_occupancy(conn, superseded_decisions)
            origin = self._insert_decision(
                conn, kind="route_change", subject_type="milestone", subject_id=milestone_id,
                subject_version=new_version, parent=None, reason=reason, actor_id=actor_id,
                request_id=request_id, detail={
                    "previous_version": int(current["version"]),
                    "requirements": [
                        {"caliber_id": c, "caliber_version": v, "required_verdicts": r}
                        for c, v, r in bound],
                    "superseded_decisions": superseded_decisions})
            self._audit(conn, actor_id=actor_id, action="milestone.revised",
                        resource_type="milestone", resource_id=milestone_id,
                        detail={"version": new_version, "decision_id": origin["decision_id"]})
            self._propagate_impact(conn, origin_id=origin["decision_id"],
                                   roots=[milestone_id], include_roots=True,
                                   reason=reason, actor_id=actor_id)
            response = {"milestone_id": milestone_id, "version": new_version,
                        "decision_id": origin["decision_id"]}
            self._store_receipt(conn, request_id=request_id, action="revise_milestone",
                                payload=payload, resource_type="milestone",
                                resource_id=milestone_id, response=response)
            return {**response, "resource_type": "milestone", "resource_id": milestone_id,
                    "replayed": False}

    # ------------------------------------------------------------ 依赖关系

    def register_dependency(self, *, request_id: str, actor_id: str, dependency_id: str,
                            upstream_milestone_id: str, downstream_milestone_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dependency_id": dependency_id,
                   "upstream_milestone_id": upstream_milestone_id,
                   "downstream_milestone_id": downstream_milestone_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            dependency_id = self._identifier(dependency_id, "dependency_id")
            upstream = self._identifier(upstream_milestone_id, "upstream_milestone_id")
            downstream = self._identifier(downstream_milestone_id, "downstream_milestone_id")
            if upstream == downstream:
                raise ValidationError("里程碑不能依赖自身")
            self._load_milestone_current(conn, upstream)
            self._load_milestone_current(conn, downstream)
            if self._effective_pair_exists(conn, upstream, downstream):
                raise ConflictError("两个里程碑之间已存在有效依赖")
            if detect_cycle(upstream, downstream, lambda m: self._successors(conn, m)):
                raise ConflictError("该依赖会形成环")
            now = self._now()

            def create():
                try:
                    conn.execute("INSERT INTO dependencies(dependency_id,current_version,created_at) VALUES(?,1,?)",
                                 (dependency_id, now))
                    conn.execute(
                        "INSERT INTO dependency_versions(dependency_id,version,upstream_milestone_id,"
                        "downstream_milestone_id,status,created_by,created_at) "
                        "VALUES(?,1,?,?, 'effective',?,?)",
                        (dependency_id, upstream, downstream, actor_id, now))
                except Exception as exc:
                    raise ConflictError("依赖编号已经存在") from exc
                origin = self._insert_decision(
                    conn, kind="route_change", subject_type="dependency", subject_id=dependency_id,
                    subject_version=1, parent=None, reason="新增前置依赖", actor_id=actor_id,
                    request_id=request_id, detail={"upstream": upstream, "downstream": downstream})
                self._audit(conn, actor_id=actor_id, action="dependency.registered",
                            resource_type="dependency", resource_id=dependency_id,
                            detail={"upstream": upstream, "downstream": downstream,
                                    "decision_id": origin["decision_id"]})
                # 新前置使下游及其后继需要重新确认门有效性。
                self._propagate_impact(conn, origin_id=origin["decision_id"],
                                       roots=[downstream], include_roots=True,
                                       reason="新增前置依赖", actor_id=actor_id)
                return "dependency", dependency_id, {
                    "dependency_id": dependency_id, "version": 1,
                    "decision_id": origin["decision_id"]}

            return self._idempotent(conn, request_id=request_id, action="register_dependency",
                                    payload=payload, create=create)

    def deprecate_dependency(self, *, request_id: str, actor_id: str,
                             dependency_id: str, reason: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dependency_id": dependency_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            dependency_id = self._identifier(dependency_id, "dependency_id")
            replay = self._check_replay(conn, request_id=request_id, action="deprecate_dependency",
                                        payload=payload)
            if replay is not None:
                return replay
            row = conn.execute(
                "SELECT * FROM dependency_versions WHERE dependency_id=? AND status='effective'",
                (dependency_id,)).fetchone()
            if row is None:
                raise NotFoundError("依赖不存在或已废止")
            new_version = int(row["version"]) + 1
            reason = self._text(reason or "依赖废止", "reason")
            conn.execute("UPDATE dependency_versions SET status='superseded' WHERE dependency_id=? AND status='effective'",
                         (dependency_id,))
            conn.execute("UPDATE dependencies SET current_version=? WHERE dependency_id=?",
                         (new_version, dependency_id))
            conn.execute(
                "INSERT INTO dependency_versions(dependency_id,version,upstream_milestone_id,"
                "downstream_milestone_id,status,created_by,created_at,request_id) "
                "SELECT ?,?,upstream_milestone_id,downstream_milestone_id,'superseded',?,?,? "
                "FROM dependency_versions WHERE dependency_id=? AND version=?",
                (dependency_id, new_version, actor_id, self._now(), request_id,
                 dependency_id, row["version"]))
            origin = self._insert_decision(
                conn, kind="route_change", subject_type="dependency", subject_id=dependency_id,
                subject_version=new_version, parent=None, reason=reason, actor_id=actor_id,
                request_id=request_id,
                detail={"upstream": row["upstream_milestone_id"],
                        "downstream": row["downstream_milestone_id"], "deprecated": True})
            self._audit(conn, actor_id=actor_id, action="dependency.deprecated",
                        resource_type="dependency", resource_id=dependency_id,
                        detail={"decision_id": origin["decision_id"]})
            self._propagate_impact(conn, origin_id=origin["decision_id"],
                                   roots=[row["downstream_milestone_id"]], include_roots=True,
                                   reason=reason, actor_id=actor_id)
            response = {"dependency_id": dependency_id, "version": new_version,
                        "decision_id": origin["decision_id"]}
            self._store_receipt(conn, request_id=request_id, action="deprecate_dependency",
                                payload=payload, resource_type="dependency",
                                resource_id=dependency_id, response=response)
            return {**response, "resource_type": "dependency", "resource_id": dependency_id,
                    "replayed": False}

    # ------------------------------------------------------------ 指标口径

    def register_caliber(self, *, request_id: str, actor_id: str, caliber_id: str, name: str,
                         unit: str, domain_tag: str, rule: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "caliber_id": caliber_id, "name": name, "unit": unit,
                   "domain_tag": domain_tag, "rule": rule}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            caliber_id = self._identifier(caliber_id, "caliber_id")
            name = self._text(name, "name")
            unit = self._text(unit, "unit", 40)
            domain_tag = self._text(domain_tag, "domain_tag", 64)
            if not isinstance(rule, dict) or not rule:
                raise ValidationError("rule 必须是非空对象")
            rule_hash = digest(rule)
            compatibility_class = f"{caliber_id}:v1"
            now = self._now()

            def create():
                try:
                    conn.execute("INSERT INTO metric_calibers(caliber_id,current_version,created_at) VALUES(?,1,?)",
                                 (caliber_id, now))
                    conn.execute(
                        "INSERT INTO caliber_versions(caliber_id,version,name,unit,domain_tag,rule_hash,"
                        "compatible_previous,compatibility_class,status,created_by,created_at) "
                        "VALUES(?,1,?,?,?,?,0,?,'effective',?,?)",
                        (caliber_id, name, unit, domain_tag, rule_hash,
                         compatibility_class, actor_id, now))
                except Exception as exc:
                    raise ConflictError("口径编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="caliber.registered",
                            resource_type="caliber", resource_id=caliber_id,
                            detail={"name": name, "unit": unit, "domain_tag": domain_tag,
                                    "rule_hash": rule_hash, "compatibility_class": compatibility_class})
                return "caliber", caliber_id, {
                    "caliber_id": caliber_id, "version": 1,
                    "compatibility_class": compatibility_class}

            return self._idempotent(conn, request_id=request_id, action="register_caliber",
                                    payload=payload, create=create)

    def revise_caliber(self, *, request_id: str, actor_id: str, caliber_id: str, name: str,
                       unit: str, domain_tag: str, rule: dict[str, Any],
                       compatible_previous: bool) -> dict[str, Any]:
        """修订口径。compatible_previous=False 表示技术路线变更，证据不可跨用并沿依赖传播。"""
        payload = {"actor_id": actor_id, "caliber_id": caliber_id, "name": name, "unit": unit,
                   "domain_tag": domain_tag, "rule": rule, "compatible_previous": compatible_previous}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._check_replay(conn, request_id=request_id, action="revise_caliber",
                                        payload=payload)
            if replay is not None:
                return replay
            caliber_id = self._identifier(caliber_id, "caliber_id")
            name = self._text(name, "name")
            unit = self._text(unit, "unit", 40)
            domain_tag = self._text(domain_tag, "domain_tag", 64)
            if not isinstance(rule, dict) or not rule:
                raise ValidationError("rule 必须是非空对象")
            current = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND status='effective'",
                (caliber_id,)).fetchone()
            if current is None:
                raise NotFoundError("口径不存在")
            new_version = int(current["version"]) + 1
            if compatible_previous:
                compatibility_class = current["compatibility_class"]
            else:
                compatibility_class = f"{caliber_id}:v{new_version}"
            rule_hash = digest(rule)
            now = self._now()
            conn.execute("UPDATE caliber_versions SET status='superseded' WHERE caliber_id=? AND status='effective'",
                         (caliber_id,))
            conn.execute("UPDATE metric_calibers SET current_version=? WHERE caliber_id=?",
                         (new_version, caliber_id))
            conn.execute(
                "INSERT INTO caliber_versions(caliber_id,version,name,unit,domain_tag,rule_hash,"
                "compatible_previous,compatibility_class,status,created_by,created_at,request_id) "
                "VALUES(?,?,?,?,?,?,?,?, 'effective',?,?,?)",
                (caliber_id, new_version, name, unit, domain_tag, rule_hash,
                 1 if compatible_previous else 0, compatibility_class, actor_id, now, request_id))
            origin = self._insert_decision(
                conn, kind="route_change", subject_type="caliber", subject_id=caliber_id,
                subject_version=new_version, parent=None,
                reason="口径兼容修订" if compatible_previous else "口径不兼容修订（技术路线变更）",
                actor_id=actor_id, request_id=request_id, detail={
                    "compatible_previous": compatible_previous,
                    "compatibility_class": compatibility_class,
                    "previous_class": current["compatibility_class"]})
            self._audit(conn, actor_id=actor_id, action="caliber.revised",
                        resource_type="caliber", resource_id=caliber_id,
                        detail={"version": new_version, "compatible_previous": compatible_previous,
                                "compatibility_class": compatibility_class,
                                "decision_id": origin["decision_id"]})
            if not compatible_previous:
                roots = self._milestones_referencing_caliber(conn, caliber_id)
                self._propagate_impact(conn, origin_id=origin["decision_id"], roots=roots,
                                       include_roots=True,
                                       reason="上游口径不兼容变更，需重新确认交付物",
                                       actor_id=actor_id)
            response = {"caliber_id": caliber_id, "version": new_version,
                        "compatibility_class": compatibility_class,
                        "decision_id": origin["decision_id"]}
            self._store_receipt(conn, request_id=request_id, action="revise_caliber",
                                payload=payload, resource_type="caliber",
                                resource_id=caliber_id, response=response)
            return {**response, "resource_type": "caliber", "resource_id": caliber_id,
                    "replayed": False}

    # ------------------------------------------------------------ 专家与资格

    def register_expert(self, *, request_id: str, actor_id: str, expert_actor_id: str,
                        display_name: str, organization_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "expert_actor_id": expert_actor_id,
                   "display_name": display_name, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            target = conn.execute("SELECT * FROM actors WHERE actor_id=?", (expert_actor_id,)).fetchone()
            if target is None:
                raise NotFoundError("专家对应的操作者不存在")
            if target["role"] != "reviewer":
                raise ValidationError("只有 reviewer 角色可以登记为验收专家")
            organization_id = self._identifier(organization_id, "organization_id")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("机构不存在")
            display_name = self._text(display_name, "display_name")
            expert_id = uuid.uuid5(NAMESPACE, f"expert:{expert_actor_id}").hex

            def create():
                try:
                    conn.execute(
                        "INSERT INTO experts(expert_id,actor_id,display_name,organization_id,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (expert_id, expert_actor_id, display_name, organization_id, self._now()))
                except Exception as exc:
                    raise ConflictError("该操作者已是专家") from exc
                self._audit(conn, actor_id=actor_id, action="expert.registered",
                            resource_type="expert", resource_id=expert_id,
                            detail={"expert_actor_id": expert_actor_id,
                                    "organization_id": organization_id})
                return "expert", expert_id, {"expert_id": expert_id}

            return self._idempotent(conn, request_id=request_id, action="register_expert",
                                    payload=payload, create=create)

    def add_qualification(self, *, request_id: str, actor_id: str, expert_actor_id: str,
                          domain_tag: str, valid_from: str, valid_until: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "expert_actor_id": expert_actor_id,
                   "domain_tag": domain_tag, "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            expert = self._expert_by_actor(conn, expert_actor_id)
            domain_tag = self._text(domain_tag, "domain_tag", 64)
            valid_from = self._text(valid_from, "valid_from", 40)
            valid_until = self._text(valid_until, "valid_until", 40)
            if not (valid_from <= valid_until):
                raise ValidationError("有效期起止顺序无效")
            qualification_id = uuid.uuid4().hex

            def create():
                conn.execute(
                    "INSERT INTO expert_qualifications(qualification_id,expert_id,domain_tag,valid_from,"
                    "valid_until,status,created_by,created_at) VALUES(?,?,?,?,?, 'active',?,?)",
                    (qualification_id, expert["expert_id"], domain_tag, valid_from, valid_until,
                     actor_id, self._now()))
                self._audit(conn, actor_id=actor_id, action="qualification.added",
                            resource_type="expert_qualification", resource_id=qualification_id,
                            detail={"expert_actor_id": expert_actor_id, "domain_tag": domain_tag,
                                    "valid_from": valid_from, "valid_until": valid_until})
                return ("expert_qualification", qualification_id,
                        {"qualification_id": qualification_id})

            return self._idempotent(conn, request_id=request_id, action="add_qualification",
                                    payload=payload, create=create)

    def revoke_qualification(self, *, request_id: str, actor_id: str, qualification_id: str,
                             reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "qualification_id": qualification_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            qualification_id = self._identifier(qualification_id, "qualification_id")
            reason = self._text(reason, "reason")
            replay = self._check_replay(conn, request_id=request_id,
                                        action="revoke_qualification", payload=payload)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM expert_qualifications WHERE qualification_id=?",
                               (qualification_id,)).fetchone()
            if row is None:
                raise NotFoundError("资格不存在")
            if row["status"] != "active":
                raise StateError("资格已被撤销")

            def create():
                conn.execute(
                    "UPDATE expert_qualifications SET status='revoked',revoked_reason=?,revoked_at=? "
                    "WHERE qualification_id=?",
                    (reason, self._now(), qualification_id))
                self._audit(conn, actor_id=actor_id, action="qualification.revoked",
                            resource_type="expert_qualification", resource_id=qualification_id,
                            detail={"reason": reason})
                return "expert_qualification", qualification_id, {"qualification_id": qualification_id}

            return self._idempotent(conn, request_id=request_id, action="revoke_qualification",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 证据包

    def submit_evidence(self, *, request_id: str, actor_id: str, caliber_id: str,
                        caliber_version: int | None, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        payload = {"actor_id": actor_id, "caliber_id": caliber_id,
                   "caliber_version": caliber_version, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *EVIDENCE_ROLES)
            caliber_id = self._identifier(caliber_id, "caliber_id")
            version_row = self._caliber_version(conn, caliber_id, caliber_version)
            evidence_id = uuid.uuid4().hex
            payload_hash = digest(payload["payload"])
            now = self._now()

            def create():
                conn.execute(
                    "INSERT INTO evidence_packages(evidence_id,caliber_id,caliber_version,"
                    "compatibility_class,payload_hash,status,submitted_by,created_at,request_id) "
                    "VALUES(?,?,?,?,?, 'available',?,?,?)",
                    (evidence_id, caliber_id, version_row["version"],
                     version_row["compatibility_class"], payload_hash, actor_id, now, request_id))
                self._audit(conn, actor_id=actor_id, action="evidence.submitted",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"caliber_id": caliber_id,
                                    "caliber_version": version_row["version"],
                                    "compatibility_class": version_row["compatibility_class"],
                                    "payload_hash": payload_hash})
                return "evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(conn, request_id=request_id, action="submit_evidence",
                                    payload=payload, create=create)

    def void_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                      reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "evidence_id": evidence_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            evidence_id = self._identifier(evidence_id, "evidence_id")
            replay = self._check_replay(conn, request_id=request_id, action="void_evidence",
                                        payload=payload)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM evidence_packages WHERE evidence_id=?",
                               (evidence_id,)).fetchone()
            if row is None:
                raise NotFoundError("证据不存在")
            if row["status"] != "available":
                raise StateError("证据已作废")
            reason = self._text(reason, "reason")

            def create():
                conn.execute("UPDATE evidence_packages SET status='void' WHERE evidence_id=?",
                             (evidence_id,))
                origin = self._insert_decision(
                    conn, kind="withdrawal", subject_type="evidence", subject_id=evidence_id,
                    subject_version=None, parent=None, reason=reason, actor_id=actor_id,
                    request_id=request_id, detail={"caliber_id": row["caliber_id"],
                                                   "caliber_version": row["caliber_version"]})
                self._audit(conn, actor_id=actor_id, action="evidence.voided",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"reason": reason, "decision_id": origin["decision_id"]})
                self._ripple_verdicts_for_evidence(conn, evidence_id, origin["decision_id"], actor_id)
                return "evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(conn, request_id=request_id, action="void_evidence",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 独立验收结论

    def submit_verdict(self, *, request_id: str, actor_id: str, milestone_id: str,
                       caliber_id: str, conclusion: str, evidence_ids: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "milestone_id": milestone_id, "caliber_id": caliber_id,
                   "conclusion": conclusion, "evidence_ids": evidence_ids}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            milestone_id = self._identifier(milestone_id, "milestone_id")
            caliber_id = self._identifier(caliber_id, "caliber_id")
            if conclusion not in ("pass", "fail"):
                raise ValidationError("conclusion 必须是 pass 或 fail")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise ValidationError("evidence_ids 必须是非空数组")
            milestone = self._load_milestone_current(conn, milestone_id)
            if milestone["status"] == "terminated":
                raise StateError("里程碑已随课题终止，不能验收")
            requirement = conn.execute(
                "SELECT * FROM milestone_requirements WHERE milestone_id=? AND version=? AND caliber_id=?",
                (milestone_id, milestone["version"], caliber_id)).fetchone()
            if requirement is None:
                raise ValidationError("该口径不属于里程碑当前版本的验收要求")
            caliber = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND version=?",
                (caliber_id, requirement["caliber_version"])).fetchone()
            expert = self._expert_by_actor(conn, actor_id)
            project = self._project_of_milestone(conn, milestone_id)
            if expert["organization_id"] == project["undertaking_org_id"]:
                raise PermissionDenied("验收专家必须独立于课题承担机构")
            qualification = self._valid_qualification(conn, expert["expert_id"],
                                                      caliber["domain_tag"])
            occupied: list[str] = []
            seen_evidence: set[str] = set()
            for evidence_id in evidence_ids:
                evidence_id = self._identifier(evidence_id, "evidence_id")
                if evidence_id in seen_evidence:
                    raise ValidationError("证据列表存在重复")
                seen_evidence.add(evidence_id)
                evidence = conn.execute("SELECT * FROM evidence_packages WHERE evidence_id=?",
                                        (evidence_id,)).fetchone()
                if evidence is None:
                    raise NotFoundError(f"证据 {evidence_id} 不存在")
                if evidence["status"] != "available":
                    raise StateError(f"证据 {evidence_id} 已作废")
                if evidence["compatibility_class"] != caliber["compatibility_class"]:
                    raise ConflictError(
                        f"证据 {evidence_id} 的口径兼容类与验收要求不兼容，不能跨口径占用")
                occupancy = conn.execute(
                    "SELECT decision_id FROM evidence_occupancy WHERE evidence_id=? AND status='active'",
                    (evidence_id,)).fetchone()
                if occupancy is not None:
                    occupied.append(evidence_id)
            if occupied:
                raise ConflictError(f"证据已被其他阶段门占用：{', '.join(sorted(occupied))}")
            verdict_id = uuid.uuid4().hex
            now = self._now()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO acceptance_verdicts(verdict_id,request_id,milestone_id,"
                        "milestone_version,caliber_id,caliber_version,expert_actor_id,qualification_id,"
                        "conclusion,evidence_json,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,'effective',?,?)",
                        (verdict_id, request_id, milestone_id, milestone["version"], caliber_id,
                         requirement["caliber_version"], actor_id, qualification["qualification_id"],
                         conclusion, canonical_json(sorted(seen_evidence)), actor_id, now))
                except Exception as exc:
                    raise ConflictError("该专家对同一里程碑版本与口径已有有效结论") from exc
                self._audit(conn, actor_id=actor_id, action="verdict.submitted",
                            resource_type="verdict", resource_id=verdict_id,
                            detail={"milestone_id": milestone_id,
                                    "milestone_version": milestone["version"],
                                    "caliber_id": caliber_id,
                                    "caliber_version": requirement["caliber_version"],
                                    "conclusion": conclusion,
                                    "evidence": sorted(seen_evidence)})
                return "verdict", verdict_id, {"verdict_id": verdict_id}

            return self._idempotent(conn, request_id=request_id, action="submit_verdict",
                                    payload=payload, create=create)

    def withdraw_verdict(self, *, request_id: str, actor_id: str, verdict_id: str,
                         reason: str) -> dict[str, Any]:
        """撤回专家结论；若门失去有效支撑，沿依赖范围生成后继决定并回收未支付释放。"""
        payload = {"actor_id": actor_id, "verdict_id": verdict_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._check_replay(conn, request_id=request_id, action="withdraw_verdict",
                                        payload=payload)
            if replay is not None:
                return replay
            verdict_id = self._identifier(verdict_id, "verdict_id")
            verdict = conn.execute("SELECT * FROM acceptance_verdicts WHERE verdict_id=?",
                                   (verdict_id,)).fetchone()
            if verdict is None:
                raise NotFoundError("验收结论不存在")
            if verdict["status"] != "effective":
                raise StateError("结论已撤回")
            if actor["role"] != "admin" and actor["actor_id"] != verdict["expert_actor_id"]:
                raise PermissionDenied("只能由结论专家本人或管理员撤回")
            reason = self._text(reason, "reason")
            now = self._now()
            conn.execute(
                "UPDATE acceptance_verdicts SET status='withdrawn',withdrawn_at=?,withdraw_reason=? "
                "WHERE verdict_id=?",
                (now, reason, verdict_id))
            withdrawal = self._insert_decision(
                conn, kind="withdrawal", subject_type="verdict", subject_id=verdict_id,
                subject_version=None, parent=None, reason=reason, actor_id=actor_id,
                request_id=request_id, detail={
                    "milestone_id": verdict["milestone_id"],
                    "milestone_version": verdict["milestone_version"],
                    "caliber_id": verdict["caliber_id"],
                    "expert_actor_id": verdict["expert_actor_id"]})
            self._audit(conn, actor_id=actor_id, action="verdict.withdrawn",
                        resource_type="verdict", resource_id=verdict_id,
                        detail={"reason": reason, "decision_id": withdrawal["decision_id"]})
            affected = self._recompute_after_withdrawal(conn, verdict, withdrawal["decision_id"],
                                                        reason, actor)
            response = {"verdict_id": verdict_id, "decision_id": withdrawal["decision_id"],
                        "affected_gate_decisions": affected}
            self._store_receipt(conn, request_id=request_id, action="withdraw_verdict",
                                payload=payload, resource_type="verdict",
                                resource_id=verdict_id, response=response)
            return {**response, "resource_type": "verdict", "resource_id": verdict_id,
                    "replayed": False}

    # ------------------------------------------------------------ 预算分期

    def register_budget_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                             project_id: str, installments: list[dict[str, Any]],
                             note: str = "") -> dict[str, Any]:
        """登记预算计划（版本 1）。installments: [{milestone_id, amount, seq_no}]。"""
        return self._write_budget_plan(request_id=request_id, actor_id=actor_id, plan_id=plan_id,
                                       project_id=project_id, installments=installments,
                                       note=note, revise=False)

    def revise_budget_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                           installments: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
        """预算重做：旧分期作废，已支付/已关账款项保留，新分期按历史已释放净额抵扣。"""
        return self._write_budget_plan(request_id=request_id, actor_id=actor_id, plan_id=plan_id,
                                       project_id=None, installments=installments,
                                       note=note, revise=True)

    def _write_budget_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                           project_id: str | None, installments: list[dict[str, Any]],
                           note: str, revise: bool) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "project_id": project_id,
                   "installments": installments, "note": note, "revise": revise}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            plan_id = self._identifier(plan_id, "plan_id")
            note = self._text(note or ("预算调整" if revise else "初始预算计划"), "note", 500)
            if not isinstance(installments, list) or not installments:
                raise ValidationError("installments 必须是非空数组")
            parsed: list[tuple[int, str, int]] = []
            seen: set[str] = set()
            for index, item in enumerate(installments, start=1):
                if not isinstance(item, dict):
                    raise ValidationError("分期条目必须是对象")
                milestone_id = self._identifier(item.get("milestone_id", ""), "milestone_id")
                if milestone_id in seen:
                    raise ValidationError(f"里程碑 {milestone_id} 出现多个分期")
                seen.add(milestone_id)
                amount = self._amount(item.get("amount"))
                seq_no = item.get("seq_no", index)
                if not isinstance(seq_no, int) or isinstance(seq_no, bool) or seq_no < 0:
                    raise ValidationError("seq_no 必须是非负整数")
                parsed.append((seq_no, milestone_id, amount))
            parsed.sort()
            now = self._now()

            if revise:
                plan_row = conn.execute("SELECT * FROM budget_plans WHERE plan_id=?",
                                        (plan_id,)).fetchone()
                if plan_row is None:
                    raise NotFoundError("预算计划不存在")
                project_id = plan_row["project_id"]
                old_version = int(plan_row["current_version"])
                new_version = old_version + 1
            else:
                project_id = self._identifier(project_id or "", "project_id")
                if conn.execute("SELECT 1 FROM projects WHERE project_id=?",
                                (project_id,)).fetchone() is None:
                    raise NotFoundError("课题不存在")
                new_version = 1
            project_milestones = self._project_milestone_ids(conn, project_id)
            for _, milestone_id, _ in parsed:
                if milestone_id not in project_milestones:
                    raise ValidationError(f"里程碑 {milestone_id} 不属于该课题")

            def create():
                if revise:
                    conn.execute(
                        "UPDATE budget_plan_versions SET status='superseded' WHERE plan_id=? AND version=?",
                        (plan_id, old_version))
                    conn.execute(
                        "UPDATE installments SET status='superseded' WHERE plan_id=? AND plan_version=?",
                        (plan_id, old_version))
                    conn.execute("UPDATE budget_plans SET current_version=? WHERE plan_id=?",
                                 (new_version, plan_id))
                else:
                    try:
                        conn.execute(
                            "INSERT INTO budget_plans(plan_id,project_id,current_version,created_at) "
                            "VALUES(?,?,1,?)",
                            (plan_id, project_id, now))
                    except Exception as exc:
                        raise ConflictError("预算计划编号已经存在") from exc
                conn.execute(
                    "INSERT INTO budget_plan_versions(plan_id,version,status,note,created_by,"
                    "created_at,request_id) VALUES(?,?,'effective',?,?,?,?)",
                    (plan_id, new_version, note, actor_id, now, request_id))
                for seq_no, milestone_id, amount in parsed:
                    conn.execute(
                        "INSERT INTO installments(installment_id,plan_id,plan_version,seq_no,"
                        "milestone_id,amount,released_amount,status) VALUES(?,?,?,?,?,?,0,'scheduled')",
                        (uuid.uuid4().hex, plan_id, new_version, seq_no, milestone_id, amount))
                origin = self._insert_decision(
                    conn, kind="route_change" if revise else "impact",
                    subject_type="budget_plan", subject_id=plan_id,
                    subject_version=new_version, parent=None, reason=note, actor_id=actor_id,
                    request_id=request_id,
                    detail={"project_id": project_id, "revise": revise,
                            "installments": [
                                {"milestone_id": m, "amount": a} for _, m, a in parsed]})
                self._audit(conn, actor_id=actor_id,
                            action="budget_plan.revised" if revise else "budget_plan.registered",
                            resource_type="budget_plan", resource_id=plan_id,
                            detail={"version": new_version, "decision_id": origin["decision_id"]})
                return "budget_plan", plan_id, {"plan_id": plan_id, "version": new_version}

            return self._idempotent(conn, request_id=request_id,
                                    action="revise_budget_plan" if revise else "register_budget_plan",
                                    payload=payload, create=create)

    def pay_release(self, *, request_id: str, actor_id: str, release_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "release_id": release_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            release_id = self._identifier(release_id, "release_id")
            replay = self._check_replay(conn, request_id=request_id, action="pay_release",
                                        payload=payload)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM budget_releases WHERE release_id=?",
                               (release_id,)).fetchone()
            if row is None:
                raise NotFoundError("额度释放记录不存在")
            if row["status"] != "released":
                raise StateError("释放已被撤回，不能支付")
            if conn.execute("SELECT 1 FROM payments WHERE release_id=?", (release_id,)).fetchone():
                raise ConflictError("该释放已支付")
            payment_id = uuid.uuid4().hex
            now = self._now()

            def create():
                conn.execute(
                    "INSERT INTO payments(payment_id,release_id,amount,status,request_id,created_by,"
                    "paid_at) VALUES(?,?,?, 'paid',?,?,?)",
                    (payment_id, release_id, row["amount"], request_id, actor_id, now))
                self._audit(conn, actor_id=actor_id, action="payment.paid",
                            resource_type="payment", resource_id=payment_id,
                            detail={"release_id": release_id, "amount": row["amount"]})
                return "payment", payment_id, {"payment_id": payment_id}

            return self._idempotent(conn, request_id=request_id, action="pay_release",
                                    payload=payload, create=create)

    def close_payment(self, *, request_id: str, actor_id: str, payment_id: str) -> dict[str, Any]:
        """关账支付；关账后的款项永远不能被撤回或回收。"""
        payload = {"actor_id": actor_id, "payment_id": payment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            payment_id = self._identifier(payment_id, "payment_id")
            replay = self._check_replay(conn, request_id=request_id, action="close_payment",
                                        payload=payload)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            if row is None:
                raise NotFoundError("支付不存在")
            if row["status"] != "paid":
                raise StateError("支付已关账或已冲正")
            now = self._now()

            def create():
                conn.execute("UPDATE payments SET status='closed',closed_at=? WHERE payment_id=?",
                             (now, payment_id))
                self._audit(conn, actor_id=actor_id, action="payment.closed",
                            resource_type="payment", resource_id=payment_id,
                            detail={"release_id": row["release_id"], "amount": row["amount"]})
                return "payment", payment_id, {"payment_id": payment_id, "status": "closed"}

            return self._idempotent(conn, request_id=request_id, action="close_payment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 交付承诺

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            subject_type: str, subject_id: str, title: str,
                            due_date: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "subject_type": subject_type, "subject_id": subject_id,
                   "title": title, "due_date": due_date}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            commitment_id = self._identifier(commitment_id, "commitment_id")
            if subject_type not in ("project", "work_package", "milestone"):
                raise ValidationError("subject_type 无效")
            subject_id = self._identifier(subject_id, "subject_id")
            self._require_subject_exists(conn, subject_type, subject_id)
            title = self._text(title, "title")
            if due_date is not None:
                due_date = self._text(due_date, "due_date", 40)
            now = self._now()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO delivery_commitments(commitment_id,subject_type,subject_id,"
                        "current_version,created_at) VALUES(?,?,?,1,?)",
                        (commitment_id, subject_type, subject_id, now))
                    conn.execute(
                        "INSERT INTO commitment_versions(commitment_id,version,title,due_date,status,"
                        "created_by,created_at) VALUES(?,1,?,?, 'effective',?,?)",
                        (commitment_id, title, due_date, actor_id, now))
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="commitment.registered",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"subject_type": subject_type, "subject_id": subject_id,
                                    "title": title, "due_date": due_date})
                return "commitment", commitment_id, {"commitment_id": commitment_id, "version": 1}

            return self._idempotent(conn, request_id=request_id, action="register_commitment",
                                    payload=payload, create=create)

    # ------------------------------------------------------------ 阶段门

    def evaluate_gate(self, milestone_id: str, actor_id: str | None = None) -> GateEvaluation:
        """只读评估阶段门，返回每一项确切阻塞原因与可释放额度。"""
        with self.database.transaction() as conn:
            if actor_id:
                self._actor(conn, actor_id)
            return self._evaluate(conn, milestone_id)

    def decide_gate(self, *, request_id: str, actor_id: str, milestone_id: str) -> dict[str, Any]:
        """原子过门：前置有效且独立验收有效时，单事务落决定、占证据、释放额度。"""
        payload = {"actor_id": actor_id, "milestone_id": milestone_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            milestone_id = self._identifier(milestone_id, "milestone_id")

            def create():
                evaluation = self._evaluate(conn, milestone_id)
                if not (evaluation.passed or evaluation.partial):
                    codes = {b.code for b in evaluation.blocks}
                    if {"milestone_terminated"} & codes:
                        raise StateError("里程碑已随课题终止")
                    raise PreconditionError(
                        "阶段门条件未满足，不能放行",
                        blocks=[b.__dict__ for b in evaluation.blocks])
                result = self._apply_gate_result(conn, evaluation=evaluation,
                                                 reason="阶段门原子通过" if evaluation.passed
                                                 else "阶段门部分通过，剩余口径限期整改",
                                                 actor_id=actor_id, request_id=request_id)
                return "gate_decision", result["decision_id"], result

            return self._idempotent(conn, request_id=request_id, action="decide_gate",
                                    payload=payload, create=create)

    def open_rectification(self, *, request_id: str, actor_id: str, milestone_id: str,
                           due_date: str, items: list[str]) -> dict[str, Any]:
        """对部分通过的阶段门开具限期整改。"""
        payload = {"actor_id": actor_id, "milestone_id": milestone_id,
                   "due_date": due_date, "items": items}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            milestone_id = self._identifier(milestone_id, "milestone_id")
            due_date = self._text(due_date, "due_date", 40)
            if not isinstance(items, list) or not items or not all(isinstance(i, str) and i.strip() for i in items):
                raise ValidationError("items 必须是非空字符串数组")
            replay = self._check_replay(conn, request_id=request_id, action="open_rectification",
                                        payload=payload)
            if replay is not None:
                return replay
            milestone = self._load_milestone_current(conn, milestone_id)
            gate = conn.execute(
                "SELECT * FROM decisions WHERE subject_type='milestone' AND subject_id=? "
                "AND subject_version=? AND kind IN ('gate_pass','gate_partial') AND status='effective' "
                "ORDER BY occurred_at DESC, decision_id DESC LIMIT 1",
                (milestone_id, milestone["version"])).fetchone()
            if gate is None:
                raise StateError("当前没有可挂接整改的有效阶段门结论")
            evaluation = self._evaluate(conn, milestone_id)
            if evaluation.passed:
                raise StateError("阶段门已全部通过，无需整改")
            decision_id = _decision_id("rect", gate["decision_id"], request_id)
            if conn.execute("SELECT 1 FROM decisions WHERE decision_id=?",
                            (decision_id,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO decisions(decision_id,kind,subject_type,subject_id,subject_version,"
                    "parent_decision_id,reason,status,basis_hash,detail_json,request_id,created_by,"
                    "occurred_at) VALUES(?, 'rectification','milestone',?,?,?,?,'effective',?,?,?,?,?)",
                    (decision_id, milestone_id, milestone["version"], gate["decision_id"],
                     f"限期整改至 {due_date}", digest({"items": items, "due_date": due_date}),
                     canonical_json({"due_date": due_date, "items": items,
                                     "blocks": [b.__dict__ for b in evaluation.blocks]}),
                     request_id, actor_id, self._now()))
                self._insert_closure(conn, gate["decision_id"], decision_id, 1)
                self._audit(conn, actor_id=actor_id, action="rectification.opened",
                            resource_type="decision", resource_id=decision_id,
                            detail={"milestone_id": milestone_id, "due_date": due_date,
                                    "items": items, "parent_decision_id": gate["decision_id"]})
            response = {"decision_id": decision_id, "milestone_id": milestone_id,
                        "due_date": due_date, "items": items}
            self._store_receipt(conn, request_id=request_id, action="open_rectification",
                                payload=payload, resource_type="decision",
                                resource_id=decision_id, response=response)
            return {**response, "resource_type": "decision", "resource_id": decision_id,
                    "replayed": False}

    def terminate_project(self, *, request_id: str, actor_id: str, project_id: str,
                          reason: str) -> dict[str, Any]:
        """课题终止：沿工作包/依赖范围生成后继决定，未释放额度注销，已支付款项保留。"""
        payload = {"actor_id": actor_id, "project_id": project_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            project_id = self._identifier(project_id, "project_id")
            reason = self._text(reason, "reason")
            replay = self._check_replay(conn, request_id=request_id, action="terminate_project",
                                        payload=payload)
            if replay is not None:
                return replay
            current = conn.execute(
                "SELECT * FROM project_versions WHERE project_id=? AND status IN ('effective','terminated') "
                "ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
            if current is None:
                raise NotFoundError("课题不存在")
            if current["status"] == "terminated":
                raise StateError("课题已经终止")
            new_version = int(current["version"]) + 1
            now = self._now()
            conn.execute(
                "UPDATE project_versions SET status='superseded' WHERE project_id=? AND status='effective'",
                (project_id,))
            conn.execute("UPDATE projects SET current_version=? WHERE project_id=?",
                         (new_version, project_id))
            conn.execute(
                "INSERT INTO project_versions(project_id,version,name,undertaking_org_id,status,"
                "created_by,created_at,request_id) VALUES(?, ?,?,?,'terminated',?,?,?)",
                (project_id, new_version, current["name"], current["undertaking_org_id"],
                 actor_id, now, request_id))
            origin = self._insert_decision(
                conn, kind="termination", subject_type="project", subject_id=project_id,
                subject_version=new_version, parent=None, reason=reason, actor_id=actor_id,
                request_id=request_id, detail={"previous_version": int(current["version"])})
            milestone_ids = self._project_milestone_ids(conn, project_id)
            # 工作包整体终止（每个工作包只生成一个终止版本）。
            wp_ids = {r["wp_id"] for r in conn.execute(
                "SELECT DISTINCT wp_id FROM milestones WHERE wp_id IN "
                "(SELECT wp_id FROM work_packages WHERE project_id=?)", (project_id,))}
            for wp_id in sorted(wp_ids):
                wp = conn.execute("SELECT * FROM work_packages WHERE wp_id=?", (wp_id,)).fetchone()
                wp_current = conn.execute(
                    "SELECT * FROM wp_versions WHERE wp_id=? ORDER BY version DESC LIMIT 1",
                    (wp_id,)).fetchone()
                if wp_current["status"] != "terminated":
                    conn.execute(
                        "UPDATE wp_versions SET status='superseded' WHERE wp_id=? AND status='effective'",
                        (wp_id,))
                    wp_new = int(wp["current_version"]) + 1
                    conn.execute("UPDATE work_packages SET current_version=? WHERE wp_id=?",
                                 (wp_new, wp_id))
                    conn.execute(
                        "INSERT INTO wp_versions(wp_id,version,name,status,created_by,created_at,"
                        "request_id) VALUES(?,?,'<terminated>','terminated',?,?,?)",
                        (wp_id, wp_new, actor_id, now, request_id))
            cancelled: list[str] = []
            for milestone_id in sorted(milestone_ids):
                mrow = self._load_milestone_current(conn, milestone_id)
                wp_id = conn.execute("SELECT wp_id FROM milestones WHERE milestone_id=?",
                                     (milestone_id,)).fetchone()["wp_id"]
                if mrow["status"] != "terminated":
                    conn.execute(
                        "UPDATE milestone_versions SET status='terminated' WHERE milestone_id=? AND version=?",
                        (milestone_id, mrow["version"]))
                gates = self._supersede_gate_decisions(conn, milestone_id=milestone_id,
                                                       milestone_version=int(mrow["version"]))
                self._free_occupancy(conn, gates)
                # 回收该里程碑已释放但尚未支付/关账的款项；已支付/关账部分保留。
                self._revoke_releases_to_target(
                    conn, old_gate_ids=gates, milestone_id=milestone_id, target=0,
                    by_decision_id=origin["decision_id"],
                    installment=self._current_installment(conn, milestone_id),
                    already=self._already_released(conn, milestone_id))
                cancel_id = self._cancel_unreleased(conn, project_id=project_id,
                                                    milestone_id=milestone_id,
                                                    parent_id=origin["decision_id"],
                                                    reason=reason, actor_id=actor_id)
                if cancel_id:
                    cancelled.append(cancel_id)
            self._impact_commitments(
                conn, origin_id=origin["decision_id"],
                subjects=([("milestone", m) for m in sorted(milestone_ids)]
                          + [("work_package", w) for w in sorted(wp_ids)]
                          + [("project", project_id)]),
                new_status="cancelled", reason=reason, actor_id=actor_id)
            self._audit(conn, actor_id=actor_id, action="project.terminated",
                        resource_type="project", resource_id=project_id,
                        detail={"version": new_version, "decision_id": origin["decision_id"],
                                "milestones": sorted(milestone_ids),
                                "cancelled_installment_decisions": cancelled})
            response = {"project_id": project_id, "version": new_version,
                        "decision_id": origin["decision_id"], "cancelled_decisions": cancelled}
            self._store_receipt(conn, request_id=request_id, action="terminate_project",
                                payload=payload, resource_type="project",
                                resource_id=project_id, response=response)
            return {**response, "resource_type": "project", "resource_id": project_id,
                    "replayed": False}

    # ============================================================== 门引擎

    def _evaluate(self, conn, milestone_id: str) -> GateEvaluation:
        milestone = conn.execute(
            "SELECT mv.*, m.wp_id FROM milestone_versions mv JOIN milestones m USING(milestone_id) "
            "WHERE milestone_id=? AND version=(SELECT current_version FROM milestones WHERE milestone_id=?)",
            (milestone_id, milestone_id)).fetchone()
        if milestone is None:
            raise NotFoundError("里程碑不存在")
        blocks: list[GateBlock] = []
        version = int(milestone["version"])
        hard_blocked = milestone["status"] == "terminated"
        if hard_blocked:
            blocks.append(GateBlock("milestone_terminated", "里程碑已随课题终止", milestone_id))
        incompatible_outdated = False

        # 本里程碑当前版本已有的有效门（评估时忽略其证据占用——重放/重算时它们会在同一事务被替代）。
        own_gate_ids = {r["decision_id"] for r in conn.execute(
            "SELECT decision_id FROM decisions WHERE subject_type='milestone' AND subject_id=? "
            "AND subject_version=? AND kind IN ('gate_pass','gate_partial') AND status='effective'",
            (milestone_id, version))}

        # 1) 前置里程碑：当前版本必须已有原子全通过的门决定。
        prerequisites: list[str] = []
        prerequisites_failed = False
        if not hard_blocked:
            for dep in conn.execute(
                    "SELECT * FROM dependency_versions WHERE downstream_milestone_id=? "
                    "AND status='effective'", (milestone_id,)):
                upstream_gate = conn.execute(
                    "SELECT decision_id FROM decisions WHERE subject_type='milestone' "
                    "AND subject_id=? AND status='effective' AND kind='gate_pass' "
                    "AND subject_version=(SELECT current_version FROM milestones WHERE milestone_id=?) "
                    "ORDER BY occurred_at DESC, decision_id DESC LIMIT 1",
                    (dep["upstream_milestone_id"], dep["upstream_milestone_id"])).fetchone()
                if upstream_gate is None:
                    prerequisites_failed = True
                    blocks.append(GateBlock(
                        "prerequisite_not_passed",
                        f"前置里程碑 {dep['upstream_milestone_id']} 当前版本尚未通过阶段门",
                        dep["upstream_milestone_id"]))
                else:
                    prerequisites.append(upstream_gate["decision_id"])

        # 2) 独立验收：每个口径要求足够的有效 pass 结论，且证据未被其他门占用。
        requirements = conn.execute(
            "SELECT * FROM milestone_requirements WHERE milestone_id=? AND version=? ORDER BY caliber_id",
            (milestone_id, version)).fetchall()
        chosen: dict[str, list[Any]] = {}
        requirement_status: list[dict[str, Any]] = []
        for requirement in requirements:
            caliber = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND version=?",
                (requirement["caliber_id"], requirement["caliber_version"])).fetchone()
            current_caliber = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND status='effective'",
                (requirement["caliber_id"],)).fetchone()
            outdated_incompatible = (
                not hard_blocked and current_caliber is not None
                and current_caliber["compatibility_class"] != caliber["compatibility_class"])
            if outdated_incompatible:
                incompatible_outdated = True
                blocks.append(GateBlock(
                    "caliber_incompatible_outdated",
                    f"口径 {requirement['caliber_id']} 已发生不兼容修订（当前 v"
                    f"{current_caliber['version']}），里程碑须修订到新口径后重新验收",
                    requirement["caliber_id"]))
            candidates = conn.execute(
                "SELECT * FROM acceptance_verdicts WHERE milestone_id=? AND milestone_version=? "
                "AND caliber_id=? AND caliber_version=? AND conclusion='pass' AND status='effective' "
                "ORDER BY created_at, verdict_id",
                (milestone_id, version, requirement["caliber_id"],
                 requirement["caliber_version"])).fetchall()
            usable: list[Any] = []
            seen_experts: set[str] = set()
            busy_evidence: set[str] = set()
            for verdict in candidates:
                if verdict["expert_actor_id"] in seen_experts:
                    continue
                evidence_ids = self._verdict_evidence(verdict)
                blocked_by = ""
                for evidence_id in evidence_ids:
                    occupancy = conn.execute(
                        "SELECT decision_id FROM evidence_occupancy WHERE evidence_id=? "
                        "AND status='active'", (evidence_id,)).fetchone()
                    if occupancy is not None and occupancy["decision_id"] not in own_gate_ids:
                        blocked_by = occupancy["decision_id"]
                        busy_evidence.add(evidence_id)
                if not blocked_by:
                    usable.append(verdict)
                    seen_experts.add(verdict["expert_actor_id"])
            need = int(requirement["required_verdicts"])
            satisfied = len(usable) >= need
            picked = usable[:need]
            chosen[requirement["caliber_id"]] = picked
            if not satisfied and not hard_blocked:
                code = "evidence_occupied" if busy_evidence else "verdicts_insufficient"
                message = (f"口径 {requirement['caliber_id']} 需要 {need} 份独立通过结论，"
                           f"当前可用 {len(usable)} 份")
                if busy_evidence:
                    message += f"；证据已被门 {sorted(busy_evidence)} 占用"
                blocks.append(GateBlock(code, message, requirement["caliber_id"]))
            requirement_status.append({
                "caliber_id": requirement["caliber_id"],
                "caliber_version": int(requirement["caliber_version"]),
                "required_verdicts": need,
                "usable_verdicts": len(usable),
                "satisfied": satisfied,
                "compatibility_class": caliber["compatibility_class"]})

        satisfied_calibers = {r["caliber_id"] for r in requirement_status if r["satisfied"]}
        all_satisfied = not any(not r["satisfied"] for r in requirement_status)
        any_satisfied = bool(satisfied_calibers)
        can_gate = not hard_blocked and not prerequisites_failed and not incompatible_outdated

        # 3) 预算分期与可释放额（按已满足口径数等比例释放，余数保留）。
        installment = None if hard_blocked else self._current_installment(conn, milestone_id)
        already = self._already_released(conn, milestone_id)
        releaseable = 0
        if installment is not None and can_gate:
            n_requirements = max(len(requirements), 1)
            if all_satisfied:
                target = installment["amount"]
            elif any_satisfied:
                target = (installment["amount"] // n_requirements) * len(satisfied_calibers)
            else:
                target = 0
            releaseable = max(target - already, 0)
        if installment is None and can_gate:
            blocks.append(GateBlock("no_installment", "该里程碑没有当前有效的预算分期", milestone_id))

        accepted = tuple(self._verdict_view(v) for verdicts in chosen.values() for v in verdicts)
        return GateEvaluation(
            milestone_id=milestone_id,
            milestone_version=version,
            passed=can_gate and all_satisfied and installment is not None,
            partial=can_gate and not all_satisfied and any_satisfied and installment is not None,
            blocks=tuple(blocks),
            satisfied_prerequisites=tuple(prerequisites),
            accepted_verdicts=accepted,
            releaseable_amount=releaseable,
            installment_id=installment["installment_id"] if installment is not None else None)

    def _apply_gate_result(self, conn, *, evaluation: GateEvaluation, reason: str,
                           actor_id: str, request_id: str | None) -> dict[str, Any]:
        kind = "gate_pass" if evaluation.passed else "gate_partial"
        old_gates = self._effective_gate_decisions(conn, evaluation.milestone_id,
                                                   evaluation.milestone_version)
        verdict_ids = sorted(v.verdict_id for v in evaluation.accepted_verdicts)
        evidence_ids = sorted({e for v in evaluation.accepted_verdicts for e in v.evidence})
        basis = digest({
            "kind": kind,
            "milestone_version": evaluation.milestone_version,
            "prerequisites": sorted(evaluation.satisfied_prerequisites),
            "verdicts": verdict_ids,
            "evidence": evidence_ids})
        decision_id = _decision_id("gate", evaluation.milestone_id,
                                   str(evaluation.milestone_version), kind, basis)
        existing = conn.execute("SELECT * FROM decisions WHERE decision_id=?",
                                (decision_id,)).fetchone()
        if existing is not None and existing["status"] == "effective":
            return {"decision_id": decision_id, "replayed": True, "released": [],
                    "releaseable_amount": 0, "kind": kind}
        for old in old_gates:
            if old["decision_id"] != decision_id:
                conn.execute("UPDATE decisions SET status='superseded' WHERE decision_id=?",
                             (old["decision_id"],))
                self._free_occupancy(conn, [old["decision_id"]])
        installment = self._current_installment(conn, evaluation.milestone_id)
        already = self._already_released(conn, evaluation.milestone_id)
        releases: list[dict[str, Any]] = []
        target = 0
        unrevokable = 0
        if installment is not None:
            n_requirements = max(int(conn.execute(
                "SELECT COUNT(*) AS c FROM milestone_requirements WHERE milestone_id=? AND version=?",
                (evaluation.milestone_id, evaluation.milestone_version)).fetchone()["c"]), 1)
            if evaluation.passed:
                target = installment["amount"]
            else:
                target = (installment["amount"] // n_requirements) * len(
                    {v.caliber_id for v in evaluation.accepted_verdicts})
            if target > already:
                releases = self._create_releases(
                    conn, installment=installment, amount=target - already,
                    decision_id=decision_id)
            elif target < already:
                unrevokable = self._revoke_releases_to_target(
                    conn, old_gate_ids=[g["decision_id"] for g in old_gates],
                    milestone_id=evaluation.milestone_id, target=target,
                    by_decision_id=decision_id, installment=installment,
                    already=already)
        for evidence_id in evidence_ids:
            conn.execute(
                "INSERT INTO evidence_occupancy(evidence_id,compatibility_class,decision_id,"
                "status,created_at) VALUES(?,?,?, 'active',?)",
                (evidence_id, f"occ:{decision_id}", decision_id, self._now()))
        detail = {
            "milestone_version": evaluation.milestone_version,
            "prerequisites": sorted(evaluation.satisfied_prerequisites),
            "verdict_ids": verdict_ids,
            "evidence_ids": evidence_ids,
            "blocks": [b.__dict__ for b in evaluation.blocks],
            "target_amount": target,
            "already_released": already,
            "releases": releases,
            "unrevokable_amount": unrevokable,
            "superseded_decisions": [g["decision_id"] for g in old_gates
                                     if g["decision_id"] != decision_id]}
        conn.execute(
            "INSERT INTO decisions(decision_id,kind,subject_type,subject_id,subject_version,"
            "parent_decision_id,reason,status,basis_hash,detail_json,request_id,created_by,occurred_at) "
            "VALUES(?,?, 'milestone',?,?,NULL,?,'effective',?,?,?,?,?)",
            (decision_id, kind, evaluation.milestone_id, evaluation.milestone_version, reason,
             basis, canonical_json(detail), request_id, actor_id, self._now()))
        self._audit(conn, actor_id=actor_id,
                    action="gate.passed" if evaluation.passed else "gate.partial",
                    resource_type="decision", resource_id=decision_id,
                    detail={"milestone_id": evaluation.milestone_id,
                            "milestone_version": evaluation.milestone_version,
                            "releases": releases, "basis_hash": basis})
        return {"decision_id": decision_id, "replayed": False, "released": releases,
                "releaseable_amount": sum(r["amount"] for r in releases), "kind": kind,
                "target_amount": target, "unrevokable_amount": unrevokable}

    # ============================================================== 级联引擎

    def _recompute_after_withdrawal(self, conn, verdict, withdrawal_id: str, reason: str,
                                    actor) -> list[str]:
        """结论撤回后重算其所在门，并在门跌落时沿依赖传播后继决定。"""
        affected: list[str] = []
        gate_rows = conn.execute(
            "SELECT * FROM decisions WHERE kind IN ('gate_pass','gate_partial') AND status='effective' "
            "AND subject_type='milestone' AND subject_id=? AND subject_version=?",
            (verdict["milestone_id"], verdict["milestone_version"])).fetchall()
        for gate in gate_rows:
            detail = self._decision_detail(gate)
            if verdict["verdict_id"] not in detail.get("verdict_ids", []):
                continue
            evaluation = self._evaluate(conn, verdict["milestone_id"])
            if evaluation.passed or evaluation.partial:
                # 仍有足够替代支撑：重算门（可能换用其他结论），资金目标按新基础重定。
                result = self._apply_gate_result(
                    conn, evaluation=evaluation,
                    reason="结论撤回后重新核算，门仍有效",
                    actor_id=actor["actor_id"], request_id=None)
                new_id = result["decision_id"]
                self._insert_closure(conn, withdrawal_id, new_id, 1)
                affected.append(new_id)
            else:
                # 门跌落：旧门失效、占用释放、未支付释放回收，并沿依赖传播。
                conn.execute("UPDATE decisions SET status='superseded' WHERE decision_id=?",
                             (gate["decision_id"],))
                self._free_occupancy(conn, [gate["decision_id"]])
                unrevokable = self._revoke_releases_to_target(
                    conn, old_gate_ids=[gate["decision_id"]],
                    milestone_id=verdict["milestone_id"], target=0,
                    by_decision_id=withdrawal_id,
                    installment=self._current_installment(conn, verdict["milestone_id"]),
                    already=self._already_released(conn, verdict["milestone_id"]))
                revocation_id = _decision_id(
                    "revoke", gate["decision_id"], withdrawal_id)
                conn.execute(
                    "INSERT INTO decisions(decision_id,kind,subject_type,subject_id,subject_version,"
                    "parent_decision_id,reason,status,basis_hash,detail_json,request_id,created_by,"
                    "occurred_at) VALUES(?, 'revocation','milestone',?,?,?,?,'effective',?,?,NULL,?,?)",
                    (revocation_id, verdict["milestone_id"], verdict["milestone_version"],
                     gate["decision_id"], f"结论撤回导致阶段门失效：{reason}",
                     digest({"withdrawal": withdrawal_id, "gate": gate["decision_id"]}),
                     canonical_json({"blocks": [b.__dict__ for b in evaluation.blocks],
                                     "unrevokable_amount": unrevokable}),
                     actor["actor_id"], self._now()))
                self._insert_closure(conn, withdrawal_id, revocation_id, 1)
                self._insert_closure(conn, gate["decision_id"], revocation_id, 1)
                self._audit(conn, actor_id=actor["actor_id"], action="gate.revoked",
                            resource_type="decision", resource_id=revocation_id,
                            detail={"gate_decision_id": gate["decision_id"],
                                    "withdrawal_id": withdrawal_id,
                                    "unrevokable_amount": unrevokable})
                affected.append(revocation_id)
                self._propagate_impact(
                    conn, origin_id=withdrawal_id,
                    roots=self._successors(conn, verdict["milestone_id"]),
                    include_roots=False,
                    reason="上游阶段门因专家结论撤回而失效", actor_id=actor["actor_id"])
        return affected

    def _ripple_verdicts_for_evidence(self, conn, evidence_id: str, origin_id: str,
                                      actor_id: str) -> None:
        """证据作废：撤回引用它的全部有效结论，再按结论撤回路径重算门。"""
        verdicts = conn.execute(
            "SELECT * FROM acceptance_verdicts WHERE status='effective' AND conclusion='pass'"
        ).fetchall()
        for verdict in verdicts:
            if evidence_id in self._verdict_evidence(verdict):
                conn.execute(
                    "UPDATE acceptance_verdicts SET status='withdrawn',withdrawn_at=?,withdraw_reason=? "
                    "WHERE verdict_id=?",
                    (self._now(), "关联证据作废，系统级联撤回", verdict["verdict_id"]))
                child = self._insert_decision(
                    conn, kind="withdrawal", subject_type="verdict",
                    subject_id=verdict["verdict_id"], subject_version=None, parent=origin_id,
                    reason="关联证据作废，系统级联撤回", actor_id=actor_id, request_id=None,
                    detail={"milestone_id": verdict["milestone_id"],
                            "milestone_version": verdict["milestone_version"],
                            "evidence_id": evidence_id})
                self._insert_closure(conn, origin_id, child["decision_id"], 1)
                self._recompute_after_withdrawal(conn, verdict, child["decision_id"],
                                                 "关联证据作废", {"actor_id": actor_id})

    def _propagate_impact(self, conn, *, origin_id: str, roots: list[str],
                          include_roots: bool, reason: str, actor_id: str) -> None:
        """沿依赖闭包为受影响里程碑/承诺生成确定性 impact 后继决定。

        roots 即受直接影响的里程碑（深度 1）；其下游依次加深。include_roots=False
        时根集合本身不建 impact 决定（根门已由调用方单独处理），只向后代传播。
        """
        depth_map: dict[str, int] = {}
        affected_projects: set[str] = set()
        affected_wps: set[str] = set()
        if include_roots:
            for node in roots:
                depth_map.setdefault(node, 1)
        frontier = list(roots)
        depth = 1
        while frontier:
            next_frontier: list[str] = []
            for node in frontier:
                for child in self._successors(conn, node):
                    if child not in depth_map:
                        depth_map[child] = depth + 1
                        next_frontier.append(child)
            frontier = next_frontier
            depth += 1
        for milestone_id, dep_depth in sorted(depth_map.items()):
            milestone = conn.execute(
                "SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,)).fetchone()
            if milestone is None:
                continue
            current = self._load_milestone_current(conn, milestone_id)
            impact_id = _decision_id("impact", origin_id, "milestone", milestone_id,
                                     str(current["version"]))
            unrevokable = 0
            if conn.execute("SELECT 1 FROM decisions WHERE decision_id=?",
                            (impact_id,)).fetchone() is None:
                gates = self._supersede_gate_decisions(
                    conn, milestone_id=milestone_id, milestone_version=int(current["version"]))
                self._free_occupancy(conn, gates)
                unrevokable = self._revoke_releases_to_target(
                    conn, old_gate_ids=gates, milestone_id=milestone_id, target=0,
                    by_decision_id=impact_id,
                    installment=self._current_installment(conn, milestone_id),
                    already=self._already_released(conn, milestone_id))
                self._insert_decision_known_id(
                    conn, decision_id=impact_id, kind="impact", subject_type="milestone",
                    subject_id=milestone_id, subject_version=int(current["version"]),
                    parent=origin_id, reason=reason, actor_id=actor_id,
                    detail={"depth": dep_depth, "superseded_decisions": gates,
                            "unrevokable_amount": unrevokable})
                self._audit(conn, actor_id=actor_id, action="impact.propagated",
                            resource_type="decision", resource_id=impact_id,
                            detail={"origin_decision_id": origin_id,
                                    "milestone_id": milestone_id, "depth": dep_depth,
                                    "unrevokable_amount": unrevokable})
            self._insert_closure(conn, origin_id, impact_id, dep_depth)
            self._impact_commitments(
                conn, origin_id=origin_id,
                subjects=[("milestone", milestone_id)],
                new_status="impacted", reason=reason, actor_id=actor_id, depth=dep_depth)
            project = self._project_of_milestone(conn, milestone_id)
            affected_projects.add(project["project_id"])
            affected_wps.add(
                conn.execute("SELECT wp_id FROM milestones WHERE milestone_id=?",
                             (milestone_id,)).fetchone()["wp_id"])
        # 工作包与课题层级的承诺在整轮传播中只影响一次。
        aggregate_subjects = [("work_package", w) for w in sorted(affected_wps)]
        aggregate_subjects += [("project", p) for p in sorted(affected_projects)]
        self._impact_commitments(conn, origin_id=origin_id, subjects=aggregate_subjects,
                                 new_status="impacted", reason=reason, actor_id=actor_id, depth=1)

    def _impact_commitments(self, conn, *, origin_id: str,
                            subjects: list[tuple[str, str]], new_status: str, reason: str,
                            actor_id: str, depth: int = 1) -> None:
        for subject_type, subject_id in sorted(set(subjects)):
            rows = conn.execute(
                "SELECT * FROM delivery_commitments WHERE subject_type=? AND subject_id=?",
                (subject_type, subject_id)).fetchall()
            for commitment in rows:
                # 同一变化来源对同一承诺只产生一次影响（无论后续版本如何）。
                already = conn.execute(
                    "SELECT 1 FROM decision_closure c JOIN decisions d ON d.decision_id=c.descendant_id "
                    "WHERE c.ancestor_id=? AND d.kind='impact' AND d.subject_type='commitment' "
                    "AND d.subject_id=? LIMIT 1",
                    (origin_id, commitment["commitment_id"])).fetchone()
                current = conn.execute(
                    "SELECT * FROM commitment_versions WHERE commitment_id=? AND version=?",
                    (commitment["commitment_id"], commitment["current_version"])).fetchone()
                if already is not None or current["status"] in ("cancelled", "fulfilled"):
                    continue
                impact_id = _decision_id("impact", origin_id, "commitment",
                                         commitment["commitment_id"], str(current["version"]))
                new_version = int(commitment["current_version"]) + 1
                conn.execute(
                    "UPDATE commitment_versions SET status='superseded' WHERE commitment_id=? AND status='effective'",
                    (commitment["commitment_id"],))
                conn.execute("UPDATE delivery_commitments SET current_version=? WHERE commitment_id=?",
                             (new_version, commitment["commitment_id"]))
                conn.execute(
                    "INSERT INTO commitment_versions(commitment_id,version,title,due_date,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (commitment["commitment_id"], new_version, current["title"],
                     current["due_date"], new_status, actor_id, self._now()))
                self._insert_decision_known_id(
                    conn, decision_id=impact_id, kind="impact", subject_type="commitment",
                    subject_id=commitment["commitment_id"], subject_version=new_version,
                    parent=origin_id, reason=reason, actor_id=actor_id,
                    detail={"depth": depth, "previous_version": current["version"],
                            "new_status": new_status,
                            "subject_type": subject_type, "subject_id": subject_id})
                self._insert_closure(conn, origin_id, impact_id, depth)
                self._audit(conn, actor_id=actor_id, action="commitment.impacted",
                            resource_type="commitment", resource_id=commitment["commitment_id"],
                            detail={"version": new_version, "status": new_status,
                                    "origin_decision_id": origin_id})

    # ------------------------------------------------------------ 资金操作

    def _create_releases(self, conn, *, installment, amount: int, decision_id: str
                         ) -> list[dict[str, Any]]:
        if amount <= 0:
            return []
        release_id = uuid.uuid4().hex
        now = self._now()
        conn.execute(
            "INSERT INTO budget_releases(release_id,installment_id,plan_version,decision_id,amount,"
            "status,created_at) VALUES(?,?,?,?,?, 'released',?)",
            (release_id, installment["installment_id"], installment["plan_version"],
             decision_id, amount, now))
        conn.execute(
            "UPDATE installments SET released_amount=released_amount+?, "
            "status=CASE WHEN released_amount+?>=amount THEN 'released' ELSE 'partially_released' END "
            "WHERE installment_id=?",
            (amount, amount, installment["installment_id"]))
        return [{"release_id": release_id, "amount": amount}]

    def _revoke_releases_to_target(self, conn, *, old_gate_ids: list[str], milestone_id: str,
                                   target: int, by_decision_id: str, installment,
                                   already: int) -> int:
        """回收指定门决定下尚未支付的释放；返回因已支付/关账而无法回收的金额。"""
        if not old_gate_ids or installment is None:
            return 0
        reclaim = already - target
        if reclaim <= 0:
            return 0
        unrecoverable = 0
        placeholders = ",".join("?" for _ in old_gate_ids)
        rows = conn.execute(
            f"SELECT br.* FROM budget_releases br JOIN installments i USING(installment_id) "
            f"WHERE i.milestone_id=? AND br.decision_id IN ({placeholders}) AND br.status='released' "
            f"ORDER BY br.created_at DESC, br.release_id DESC",
            [milestone_id, *old_gate_ids]).fetchall()
        for row in rows:
            if reclaim <= 0:
                break
            paid = conn.execute("SELECT 1 FROM payments WHERE release_id=? AND status IN ('paid','closed')",
                                (row["release_id"],)).fetchone()
            if paid is not None:
                unrecoverable += int(row["amount"])
                continue
            take = min(reclaim, int(row["amount"]))
            if take == int(row["amount"]):
                conn.execute(
                    "UPDATE budget_releases SET status='revoked',revoked_at=?,revoked_by_decision_id=? "
                    "WHERE release_id=?",
                    (self._now(), by_decision_id, row["release_id"]))
            else:
                # 部分回收：原释放保留差值，回收部分另记一条 revoked 记录以保留审计轨迹。
                conn.execute(
                    "UPDATE budget_releases SET amount=? WHERE release_id=?",
                    (int(row["amount"]) - take, row["release_id"]))
                conn.execute(
                    "INSERT INTO budget_releases(release_id,installment_id,plan_version,decision_id,"
                    "amount,status,created_at,revoked_at,revoked_by_decision_id) "
                    "VALUES(?,?,?,?,?, 'revoked',?,?,?)",
                    (uuid.uuid4().hex, row["installment_id"], row["plan_version"],
                     by_decision_id, take, self._now(), self._now(), by_decision_id))
            conn.execute(
                "UPDATE installments SET released_amount=MAX(released_amount-?,0), "
                "status=CASE WHEN released_amount-?<=0 THEN 'scheduled' ELSE 'partially_released' END "
                "WHERE installment_id=?",
                (take, take, row["installment_id"]))
            reclaim -= take
        if reclaim > 0:
            unrecoverable += reclaim
        return unrecoverable

    def _cancel_unreleased(self, conn, *, project_id: str, milestone_id: str, parent_id: str,
                           reason: str, actor_id: str) -> str | None:
        plan = conn.execute(
            "SELECT * FROM budget_plans WHERE project_id=? ORDER BY rowid LIMIT 1",
            (project_id,)).fetchone()
        if plan is None:
            return None
        rows = conn.execute(
            "SELECT * FROM installments WHERE plan_id=? AND plan_version=? AND milestone_id=? "
            "AND status IN ('scheduled','partially_released')",
            (plan["plan_id"], plan["current_version"], milestone_id)).fetchall()
        if not rows:
            return None
        for installment in rows:
            conn.execute("UPDATE installments SET status='cancelled' WHERE installment_id=?",
                         (installment["installment_id"],))
        cancel_id = _decision_id("cancel", parent_id, milestone_id)
        conn.execute(
            "INSERT INTO decisions(decision_id,kind,subject_type,subject_id,subject_version,"
            "parent_decision_id,reason,status,basis_hash,detail_json,request_id,created_by,occurred_at) "
            "VALUES(?, 'cancellation','milestone',?,NULL,?,?,'effective',?,? ,NULL,?,?)",
            (cancel_id, milestone_id, parent_id, reason,
             digest({"milestone": milestone_id}),
             canonical_json({"installments": [
                 {"installment_id": r["installment_id"],
                  "cancelled_amount": int(r["amount"]) - int(r["released_amount"])}
                 for r in rows]}),
             actor_id, self._now()))
        self._insert_closure(conn, parent_id, cancel_id, 1)
        return cancel_id

    # ------------------------------------------------------------ 读取辅助

    def _load_milestone_current(self, conn, milestone_id: str):
        row = conn.execute(
            "SELECT * FROM milestone_versions WHERE milestone_id=? AND version="
            "(SELECT current_version FROM milestones WHERE milestone_id=?)",
            (milestone_id, milestone_id)).fetchone()
        if row is None:
            raise NotFoundError("里程碑不存在")
        return row

    def _caliber_version(self, conn, caliber_id: str, version: int | None):
        if version is None:
            row = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND status='effective'",
                (caliber_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM caliber_versions WHERE caliber_id=? AND version=?",
                (caliber_id, version)).fetchone()
        if row is None:
            raise NotFoundError("指标口径版本不存在")
        return row

    def _expert_by_actor(self, conn, actor_id: str):
        row = conn.execute("SELECT * FROM experts WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise PermissionDenied("操作者没有验收专家档案")
        return row

    def _valid_qualification(self, conn, expert_id: str, domain_tag: str):
        today = self._now()[:10]
        row = conn.execute(
            "SELECT * FROM expert_qualifications WHERE expert_id=? AND domain_tag=? AND status='active' "
            "AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC, qualification_id LIMIT 1",
            (expert_id, domain_tag, today, today)).fetchone()
        if row is None:
            raise PermissionDenied(f"专家缺少 {domain_tag} 领域的有效资格")
        return row

    def _project_of_milestone(self, conn, milestone_id: str):
        row = conn.execute(
            "SELECT pv.* FROM milestones m JOIN work_packages w USING(wp_id) "
            "JOIN project_versions pv ON pv.project_id=w.project_id "
            "WHERE m.milestone_id=? AND pv.version="
            "(SELECT current_version FROM projects WHERE project_id=w.project_id)",
            (milestone_id,)).fetchone()
        if row is None:
            raise NotFoundError("里程碑所属课题不存在")
        return row

    def _project_milestone_ids(self, conn, project_id: str) -> set[str]:
        return {r["milestone_id"] for r in conn.execute(
            "SELECT m.milestone_id FROM milestones m JOIN work_packages w USING(wp_id) "
            "WHERE w.project_id=?", (project_id,))}

    def _successors(self, conn, milestone_id: str) -> list[str]:
        return [r["downstream_milestone_id"] for r in conn.execute(
            "SELECT downstream_milestone_id FROM dependency_versions "
            "WHERE upstream_milestone_id=? AND status='effective' ORDER BY downstream_milestone_id",
            (milestone_id,))]

    def _effective_pair_exists(self, conn, upstream: str, downstream: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM dependency_versions WHERE upstream_milestone_id=? "
            "AND downstream_milestone_id=? AND status='effective' LIMIT 1",
            (upstream, downstream)).fetchone() is not None

    def _milestones_referencing_caliber(self, conn, caliber_id: str) -> list[str]:
        return [r["milestone_id"] for r in conn.execute(
            "SELECT DISTINCT m.milestone_id FROM milestone_requirements r "
            "JOIN milestones m ON m.milestone_id=r.milestone_id AND m.current_version=r.version "
            "WHERE r.caliber_id=? ORDER BY r.milestone_id",
            (caliber_id,))]

    def _current_installment(self, conn, milestone_id: str):
        return conn.execute(
            "SELECT i.* FROM installments i JOIN budget_plans p USING(plan_id) "
            "WHERE p.project_id=(SELECT w.project_id FROM milestones m JOIN work_packages w USING(wp_id) "
            "WHERE m.milestone_id=?) AND i.plan_id=p.plan_id AND i.plan_version=p.current_version "
            "AND i.milestone_id=? AND i.status IN ('scheduled','partially_released','released') "
            "LIMIT 1",
            (milestone_id, milestone_id)).fetchone()

    def _already_released(self, conn, milestone_id: str) -> int:
        """当前预算计划版本下该里程碑仍然有效的释放总额（旧版计划的释放为历史，不计入新计划）。"""
        row = conn.execute(
            "SELECT COALESCE(SUM(br.amount),0) AS total FROM budget_releases br "
            "JOIN installments i ON i.installment_id=br.installment_id "
            "JOIN budget_plans p ON p.plan_id=i.plan_id AND p.current_version=i.plan_version "
            "WHERE i.milestone_id=? AND br.status='released'",
            (milestone_id,)).fetchone()
        return int(row["total"])

    def _effective_gate_decisions(self, conn, milestone_id: str, version: int):
        return conn.execute(
            "SELECT * FROM decisions WHERE subject_type='milestone' AND subject_id=? "
            "AND subject_version=? AND kind IN ('gate_pass','gate_partial') AND status='effective' "
            "ORDER BY occurred_at, decision_id",
            (milestone_id, version)).fetchall()

    def _supersede_gate_decisions(self, conn, *, milestone_id: str, milestone_version: int
                                  ) -> list[str]:
        rows = self._effective_gate_decisions(conn, milestone_id, milestone_version)
        ids = [r["decision_id"] for r in rows]
        for decision_id in ids:
            conn.execute("UPDATE decisions SET status='superseded' WHERE decision_id=?",
                         (decision_id,))
        return ids

    def _free_occupancy(self, conn, decision_ids: list[str]) -> None:
        for decision_id in decision_ids:
            conn.execute(
                "UPDATE evidence_occupancy SET status='freed',freed_at=? WHERE decision_id=? AND status='active'",
                (self._now(), decision_id))

    def _require_subject_exists(self, conn, subject_type: str, subject_id: str) -> None:
        table, column = {
            "project": ("projects", "project_id"),
            "work_package": ("work_packages", "wp_id"),
            "milestone": ("milestones", "milestone_id"),
        }[subject_type]
        if conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?",
                        (subject_id,)).fetchone() is None:
            raise NotFoundError("承诺挂载对象不存在")

    def _verdict_evidence(self, verdict_row) -> list[str]:
        import json
        return list(json.loads(verdict_row["evidence_json"]))

    def _verdict_view(self, row) -> VerdictView:
        import json
        return VerdictView(
            verdict_id=row["verdict_id"], milestone_id=row["milestone_id"],
            milestone_version=int(row["milestone_version"]), caliber_id=row["caliber_id"],
            caliber_version=int(row["caliber_version"]),
            expert_actor_id=row["expert_actor_id"],
            qualification_id=row["qualification_id"], conclusion=row["conclusion"],
            evidence=tuple(json.loads(row["evidence_json"])), status=row["status"],
            created_at=row["created_at"], withdrawn_at=row["withdrawn_at"],
            withdraw_reason=row["withdraw_reason"])

    def _decision_detail(self, row) -> dict[str, Any]:
        import json
        return json.loads(row["detail_json"])

    def _insert_decision(self, conn, *, kind: str, subject_type: str, subject_id: str,
                         subject_version: int | None, parent: str | None, reason: str,
                         actor_id: str, request_id: str | None, detail: dict[str, Any]
                         ) -> dict[str, Any]:
        decision_id = uuid.uuid4().hex
        return self._insert_decision_known_id(
            conn, decision_id=decision_id, kind=kind, subject_type=subject_type,
            subject_id=subject_id, subject_version=subject_version, parent=parent,
            reason=reason, actor_id=actor_id, request_id=request_id, detail=detail)

    def _insert_decision_known_id(self, conn, *, decision_id: str, kind: str, subject_type: str,
                                  subject_id: str, subject_version: int | None, parent: str | None,
                                  reason: str, actor_id: str, request_id: str | None = None,
                                  detail: dict[str, Any]) -> dict[str, Any]:
        conn.execute(
            "INSERT INTO decisions(decision_id,kind,subject_type,subject_id,subject_version,"
            "parent_decision_id,reason,status,basis_hash,detail_json,request_id,created_by,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,'effective',?,?,?,?,?)",
            (decision_id, kind, subject_type, subject_id, subject_version, parent, reason,
             digest(detail), canonical_json(detail), request_id, actor_id, self._now()))
        if parent is not None:
            self._insert_closure(conn, parent, decision_id, 1)
        return {"decision_id": decision_id}

    def _insert_closure(self, conn, ancestor: str, descendant: str, depth: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO decision_closure(ancestor_id,descendant_id,depth) VALUES(?,?,?)",
            (ancestor, descendant, depth))

    # ============================================================== 查询视图

    def get_milestone(self, milestone_id: str) -> MilestoneView:
        row = self.database.connection.execute(
            "SELECT mv.*, m.wp_id FROM milestone_versions mv JOIN milestones m USING(milestone_id) "
            "WHERE mv.milestone_id=? AND mv.version=m.current_version",
            (milestone_id,)).fetchone()
        if row is None:
            raise NotFoundError("里程碑不存在")
        requirements = tuple(dict(r) for r in self.database.connection.execute(
            "SELECT caliber_id,caliber_version,required_verdicts FROM milestone_requirements "
            "WHERE milestone_id=? AND version=? ORDER BY caliber_id",
            (milestone_id, row["version"])))
        return MilestoneView(
            milestone_id=milestone_id, version=row["version"], wp_id=row["wp_id"],
            name=row["name"], seq_no=row["seq_no"], planned_days=row["planned_days"],
            planned_date=row["planned_date"], status=row["status"], requirements=requirements)

    def list_milestones(self, project_id: str) -> list[MilestoneView]:
        ids = [r["milestone_id"] for r in self.database.connection.execute(
            "SELECT m.milestone_id FROM milestones m JOIN work_packages w USING(wp_id) "
            "WHERE w.project_id=? ORDER BY m.milestone_id", (project_id,))]
        return [self.get_milestone(mid) for mid in ids]

    def get_caliber(self, caliber_id: str) -> CaliberView:
        row = self.database.connection.execute(
            "SELECT * FROM caliber_versions WHERE caliber_id=? AND version="
            "(SELECT current_version FROM metric_calibers WHERE caliber_id=?)",
            (caliber_id, caliber_id)).fetchone()
        if row is None:
            raise NotFoundError("口径不存在")
        return CaliberView(
            caliber_id=caliber_id, version=row["version"], name=row["name"], unit=row["unit"],
            domain_tag=row["domain_tag"], rule_hash=row["rule_hash"],
            compatible_previous=bool(row["compatible_previous"]),
            compatibility_class=row["compatibility_class"], status=row["status"])

    def list_verdicts(self, milestone_id: str) -> list[VerdictView]:
        rows = self.database.connection.execute(
            "SELECT * FROM acceptance_verdicts WHERE milestone_id=? ORDER BY created_at, verdict_id",
            (milestone_id,)).fetchall()
        return [self._verdict_view(row) for row in rows]

    def list_decisions(self, subject_type: str | None = None, subject_id: str | None = None
                       ) -> list[DecisionView]:
        sql = "SELECT * FROM decisions"
        params: list[Any] = []
        if subject_type and subject_id:
            sql += " WHERE subject_type=? AND subject_id=?"
            params.extend([subject_type, subject_id])
        sql += " ORDER BY occurred_at, decision_id"
        rows = self.database.connection.execute(sql, params).fetchall()
        return [self._decision_view(r) for r in rows]

    def get_decision(self, decision_id: str) -> DecisionView:
        row = self.database.connection.execute("SELECT * FROM decisions WHERE decision_id=?",
                                               (decision_id,)).fetchone()
        if row is None:
            raise NotFoundError("决定不存在")
        return self._decision_view(row)

    def decision_descendants(self, decision_id: str) -> list[DecisionView]:
        rows = self.database.connection.execute(
            "SELECT d.* FROM decision_closure c JOIN decisions d ON d.decision_id=c.descendant_id "
            "WHERE c.ancestor_id=? ORDER BY c.depth, d.occurred_at, d.decision_id",
            (decision_id,)).fetchall()
        return [self._decision_view(r) for r in rows]

    def _decision_view(self, row) -> DecisionView:
        import json
        return DecisionView(
            decision_id=row["decision_id"], kind=row["kind"],
            subject_type=row["subject_type"], subject_id=row["subject_id"],
            subject_version=row["subject_version"], parent_decision_id=row["parent_decision_id"],
            reason=row["reason"], status=row["status"],
            detail=json.loads(row["detail_json"]), created_by=row["created_by"],
            occurred_at=row["occurred_at"])

    def critical_path(self, project_id: str) -> dict[str, Any]:
        """返回课题当前有效里程碑网络的关键路径与每个节点的阻塞原因。"""
        milestones = self.list_milestones(project_id)
        milestone_map: dict[str, dict[str, Any]] = {}
        for view in milestones:
            if view.status == "terminated":
                continue
            milestone_map[view.milestone_id] = {
                "milestone_id": view.milestone_id, "version": view.version, "name": view.name,
                "wp_id": view.wp_id, "seq_no": view.seq_no, "planned_days": view.planned_days}
        edge_rows = self.database.connection.execute(
            "SELECT upstream_milestone_id,downstream_milestone_id FROM dependency_versions "
            "WHERE status='effective'").fetchall()
        edges = {(r["upstream_milestone_id"], r["downstream_milestone_id"]) for r in edge_rows}
        path_rows, earliest = critical_path(milestone_map, edges)
        path_ids = [r["milestone_id"] for r in path_rows]
        nodes: list[dict[str, Any]] = []
        for milestone_id in sorted(milestone_map):
            evaluation = self.evaluate_gate(milestone_id)
            state = ("passed" if evaluation.passed
                     else "partial" if evaluation.partial else "blocked")
            blockers = [b.code for b in evaluation.blocks]
            nodes.append({
                **milestone_map[milestone_id],
                "state": state,
                "earliest_finish_days": earliest.get(milestone_id, 0),
                "on_critical_path": milestone_id in path_ids,
                "blockers": blockers})
        return {"project_id": project_id, "length_days": (
                    max(earliest.values()) if earliest else 0),
                "critical_path": path_ids, "nodes": nodes}

    def project_funds(self, project_id: str) -> dict[str, Any]:
        """返回每笔分期状态与资金尚未释放的确切原因。"""
        rows = self.database.connection.execute(
            "SELECT i.* FROM installments i JOIN budget_plans p USING(plan_id) "
            "WHERE p.project_id=? AND i.plan_version=p.current_version ORDER BY i.seq_no,i.installment_id",
            (project_id,)).fetchall()
        items = []
        for row in rows:
            released = int(row["released_amount"])
            entry = {
                "installment_id": row["installment_id"], "milestone_id": row["milestone_id"],
                "seq_no": row["seq_no"], "amount": int(row["amount"]),
                "released_amount": released, "blocked_amount": int(row["amount"]) - released,
                "status": row["status"], "reasons": [], "releaseable_amount": 0}
            if row["status"] in ("scheduled", "partially_released"):
                evaluation = self.evaluate_gate(row["milestone_id"])
                entry["reasons"] = [
                    {"code": b.code, "message": b.message, "subject": b.subject}
                    for b in evaluation.blocks]
                entry["releaseable_amount"] = evaluation.releaseable_amount
            items.append(entry)
        paid = self.database.connection.execute(
            "SELECT COALESCE(SUM(pay.amount),0) AS total FROM payments pay "
            "JOIN budget_releases br ON br.release_id=pay.release_id "
            "JOIN installments i ON i.installment_id=br.installment_id "
            "JOIN budget_plans p ON p.plan_id=i.plan_id WHERE p.project_id=?",
            (project_id,)).fetchone()["total"]
        return {"project_id": project_id, "installments": items,
                "paid_amount": int(paid)}

    def change_impact(self, decision_id: str) -> dict[str, Any]:
        """一次变化影响了哪些后继里程碑与交付承诺。"""
        origin = self.get_decision(decision_id)
        items: list[ImpactItem] = []
        for decision in self.decision_descendants(decision_id):
            if decision.kind != "impact":
                continue
            if decision.subject_type == "commitment":
                items.append(ImpactItem(
                    subject_type="commitment", subject_id=decision.subject_id,
                    commitment_id=decision.subject_id,
                    change=decision.detail.get("new_status", "impacted"),
                    decision_id=decision.decision_id))
            elif decision.subject_type == "milestone":
                linked = self.database.connection.execute(
                    "SELECT commitment_id FROM delivery_commitments WHERE subject_type='milestone' "
                    "AND subject_id=?", (decision.subject_id,)).fetchall()
                if not linked:
                    items.append(ImpactItem(
                        subject_type="milestone", subject_id=decision.subject_id,
                        commitment_id=None, change="gate_invalidated",
                        decision_id=decision.decision_id))
                for row in linked:
                    items.append(ImpactItem(
                        subject_type="milestone", subject_id=decision.subject_id,
                        commitment_id=row["commitment_id"], change="gate_invalidated",
                        decision_id=decision.decision_id))
        return {"origin_decision_id": decision_id, "origin_kind": origin.kind,
                "origin_subject": {"type": origin.subject_type, "id": origin.subject_id,
                                   "version": origin.subject_version},
                "items": [item.__dict__ for item in items]}

    def list_commitments(self, subject_type: str | None = None,
                         subject_id: str | None = None) -> list[CommitmentView]:
        sql = ("SELECT cv.*, c.subject_type, c.subject_id FROM commitment_versions cv "
               "JOIN delivery_commitments c USING(commitment_id) "
               "WHERE c.current_version=cv.version")
        params: list[Any] = []
        if subject_type and subject_id:
            sql += " AND c.subject_type=? AND c.subject_id=?"
            params.extend([subject_type, subject_id])
        sql += " ORDER BY c.commitment_id"
        rows = self.database.connection.execute(sql, params).fetchall()
        return [CommitmentView(
            commitment_id=r["commitment_id"], version=r["version"],
            subject_type=r["subject_type"], subject_id=r["subject_id"], title=r["title"],
            due_date=r["due_date"], status=r["status"]) for r in rows]

    def list_installments(self, project_id: str) -> list[InstallmentView]:
        rows = self.database.connection.execute(
            "SELECT i.* FROM installments i JOIN budget_plans p USING(plan_id) "
            "WHERE p.project_id=? AND i.plan_version=p.current_version "
            "ORDER BY i.seq_no,i.installment_id", (project_id,)).fetchall()
        return [InstallmentView(
            installment_id=r["installment_id"], plan_id=r["plan_id"],
            plan_version=r["plan_version"], seq_no=r["seq_no"], milestone_id=r["milestone_id"],
            amount=int(r["amount"]), status=r["status"],
            released_amount=int(r["released_amount"])) for r in rows]
