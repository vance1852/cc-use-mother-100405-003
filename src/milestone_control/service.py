"""项目里程碑与变更控制领域服务。

写入约定与基础服务一致：``BEGIN IMMEDIATE`` 短事务 + request_id 幂等回执 +
哈希串联审计。定义类实体（专项、课题、工作包、指标口径、证据包、专家、
里程碑、依赖、预算分期、交付承诺）只追加版本；阶段门决定、验收结论、
资金台账和变更单不可变留痕，旧版结论与已关账支付永久可审计。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from science_strategy_foundation.audit import append_event, canonical_json, digest
from science_strategy_foundation.clock import Clock, SystemClock
from science_strategy_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

from .errors import GateBlocked, ImmutableError
from .graph import caliber_compatible, critical_path, reachable_downstream
from .storage import MilestoneDatabase

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ROLES = frozenset({"admin", "operator", "reviewer", "auditor"})


class MilestoneControlService:
    """协调版本固化、阶段门原子通过、证据占用与变更传播。"""

    def __init__(self, database: MilestoneDatabase, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def verify_audit(self):
        """委托到底层审计链校验，健康检查与离线验收共用。"""

        from science_strategy_foundation.audit import verify_chain

        return verify_chain(self.database.connection)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        return value

    def _amount(self, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError("金额必须是非负整数（单位：分）")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _replay_or_none(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> dict[str, Any] | None:
        """在业务冲突校验之前识别幂等重放。"""

        request_id = self._id(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        response = json.loads(row["response_json"])
        return {"request_id": request_id, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True, **response}

    def _record_receipt(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any], resource_type: str, resource_id: str,
                        response: dict[str, Any]) -> dict[str, Any]:
        """业务写入完成后直接登记幂等回执（与 _idempotent 的 create 路径二选一）。"""

        request_id = self._id(request_id, "request_id")
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now()))
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _publish_version(self, connection, *, vtable: str, ctable: str, id_col: str,
                         entity_id: str, columns: list[str], values: list[Any],
                         current_extra: dict[str, str] | None = None) -> int:
        """追加一个版本并推进当前版本指针，返回新版本号。"""

        current = connection.execute(
            f"SELECT current_version FROM {ctable} WHERE {id_col}=?", (entity_id,)
        ).fetchone()
        version = (current["current_version"] + 1) if current else 1
        marks = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {vtable}({id_col},version,{','.join(columns)}) "
            f"VALUES(?,?,{marks})",
            (entity_id, version, *values),
        )
        if current is None:
            col_defs = f"{id_col},current_version" + (
                "," + ",".join(current_extra) if current_extra else "")
            marks = "?,?" + ("," + ",".join("?" for _ in current_extra) if current_extra else "")
            params = [entity_id, version, *(current_extra or {}).values()]
            connection.execute(
                f"INSERT INTO {ctable}({col_defs}) VALUES({marks})", params)
        else:
            connection.execute(
                f"UPDATE {ctable} SET current_version=? WHERE {id_col}=?",
                (version, entity_id))
        return version

    # ------------------------------------------------------------------
    # 定义登记：专项 / 课题 / 工作包
    # ------------------------------------------------------------------
    def register_project(self, *, request_id: str, actor_id: str, project_id: str,
                         name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        body = {"actor_id": actor_id, "project_id": project_id, "name": name, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_project", payload=body)
            if replay is not None:
                return replay
            project_id = self._id(project_id, "project_id")
            name = self._text(name, "name")
            if conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise ConflictError("专项编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_project_versions", ctable="mc_projects",
                    id_col="project_id", entity_id=project_id,
                    columns=["name", "payload_json", "created_by", "created_at"],
                    values=[name, canonical_json(payload), actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.project.registered",
                            resource_type="project", resource_id=project_id,
                            detail={"version": version, "name": name})
                return "project", project_id, {"project_id": project_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_project",
                                    payload=body, create=create)

    def revise_project(self, *, request_id: str, actor_id: str, project_id: str,
                       name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        body = {"actor_id": actor_id, "project_id": project_id, "name": name, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_project", payload=body)
            if replay is not None:
                return replay
            project_id = self._id(project_id, "project_id")
            if not conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("专项不存在")
            name = self._text(name, "name")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_project_versions", ctable="mc_projects",
                    id_col="project_id", entity_id=project_id,
                    columns=["name", "payload_json", "created_by", "created_at"],
                    values=[name, canonical_json(payload), actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.project.revised",
                            resource_type="project", resource_id=project_id,
                            detail={"version": version})
                return "project", project_id, {"project_id": project_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_project",
                                    payload=body, create=create)

    def register_topic(self, *, request_id: str, actor_id: str, topic_id: str,
                       project_id: str, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        body = {"actor_id": actor_id, "topic_id": topic_id, "project_id": project_id,
                "name": name, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_topic", payload=body)
            if replay is not None:
                return replay
            topic_id, project_id = self._id(topic_id, "topic_id"), self._id(project_id, "project_id")
            name = self._text(name, "name")
            if not conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("专项不存在")
            if conn.execute("SELECT 1 FROM mc_topics WHERE topic_id=?", (topic_id,)).fetchone():
                raise ConflictError("课题编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_topic_versions", ctable="mc_topics",
                    id_col="topic_id", entity_id=topic_id,
                    columns=["project_id", "name", "payload_json", "created_by", "created_at"],
                    values=[project_id, name, canonical_json(payload), actor_id, self._now()],
                    current_extra={"project_id": project_id})
                self._audit(conn, actor_id=actor_id, action="mc.topic.registered",
                            resource_type="topic", resource_id=topic_id,
                            detail={"project_id": project_id, "version": version})
                return "topic", topic_id, {"topic_id": topic_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_topic",
                                    payload=body, create=create)

    def register_work_package(self, *, request_id: str, actor_id: str, wp_id: str,
                              topic_id: str, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        body = {"actor_id": actor_id, "wp_id": wp_id, "topic_id": topic_id,
                "name": name, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_work_package", payload=body)
            if replay is not None:
                return replay
            wp_id, topic_id = self._id(wp_id, "wp_id"), self._id(topic_id, "topic_id")
            name = self._text(name, "name")
            topic = conn.execute("SELECT * FROM mc_topics WHERE topic_id=?", (topic_id,)).fetchone()
            if topic is None:
                raise NotFoundError("课题不存在")
            if conn.execute("SELECT 1 FROM mc_workpackages WHERE wp_id=?", (wp_id,)).fetchone():
                raise ConflictError("工作包编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_workpackage_versions", ctable="mc_workpackages",
                    id_col="wp_id", entity_id=wp_id,
                    columns=["topic_id", "name", "payload_json", "created_by", "created_at"],
                    values=[topic_id, name, canonical_json(payload), actor_id, self._now()],
                    current_extra={"topic_id": topic_id})
                self._audit(conn, actor_id=actor_id, action="mc.workpackage.registered",
                            resource_type="work_package", resource_id=wp_id,
                            detail={"topic_id": topic_id, "version": version})
                return "work_package", wp_id, {"wp_id": wp_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_work_package",
                                    payload=body, create=create)

    # ------------------------------------------------------------------
    # 指标口径与证据包
    # ------------------------------------------------------------------
    def register_metric(self, *, request_id: str, actor_id: str, metric_id: str, name: str,
                        unit: str = "", spec: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = spec or {}
        body = {"actor_id": actor_id, "metric_id": metric_id, "name": name, "unit": unit, "spec": spec}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_metric", payload=body)
            if replay is not None:
                return replay
            metric_id = self._id(metric_id, "metric_id")
            name = self._text(name, "name")
            if conn.execute("SELECT 1 FROM mc_metrics WHERE metric_id=?", (metric_id,)).fetchone():
                raise ConflictError("指标口径编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_metric_versions", ctable="mc_metrics",
                    id_col="metric_id", entity_id=metric_id,
                    columns=["name", "unit", "spec_json", "compatible", "created_by", "created_at"],
                    values=[name, unit, canonical_json(spec), 1, actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.metric.registered",
                            resource_type="metric", resource_id=metric_id,
                            detail={"version": version})
                return "metric", metric_id, {"metric_id": metric_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_metric",
                                    payload=body, create=create)

    def revise_metric(self, *, request_id: str, actor_id: str, metric_id: str, name: str,
                      unit: str = "", spec: dict[str, Any] | None = None,
                      compatible: bool = True, reason: str = "") -> dict[str, Any]:
        """发布指标口径新版本。

        compatible=False 表示技术路线口径发生不兼容变更：沿用旧口径生产的
        证据不得再被新验收占用，并自动沿依赖范围生成后继变更效果。
        """

        spec = spec or {}
        body = {"actor_id": actor_id, "metric_id": metric_id, "name": name, "unit": unit,
                "spec": spec, "compatible": compatible, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_metric", payload=body)
            if replay is not None:
                return replay
            metric_id = self._id(metric_id, "metric_id")
            name = self._text(name, "name")
            current = conn.execute("SELECT * FROM mc_metrics WHERE metric_id=?", (metric_id,)).fetchone()
            if current is None:
                raise NotFoundError("指标口径不存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_metric_versions", ctable="mc_metrics",
                    id_col="metric_id", entity_id=metric_id,
                    columns=["name", "unit", "spec_json", "compatible", "created_by", "created_at"],
                    values=[name, unit, canonical_json(spec), 1 if compatible else 0,
                            actor_id, self._now()])
                detail = {"version": version, "compatible": compatible}
                change_id = None
                if not compatible:
                    change_id = self._propagate_metric_break(conn, actor_id=actor_id,
                                                             metric_id=metric_id,
                                                             new_version=version, reason=reason)
                    detail["change_id"] = change_id
                self._audit(conn, actor_id=actor_id, action="mc.metric.revised",
                            resource_type="metric", resource_id=metric_id, detail=detail)
                return "metric", metric_id, {"metric_id": metric_id, "version": version,
                                             "change_id": change_id}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_metric",
                                    payload=body, create=create)

    def register_evidence(self, *, request_id: str, actor_id: str, evidence_id: str, title: str,
                          content_hash: str, calibers: list[dict[str, str | int]]) -> dict[str, Any]:
        body = {"actor_id": actor_id, "evidence_id": evidence_id, "title": title,
                "content_hash": content_hash, "calibers": calibers}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_evidence", payload=body)
            if replay is not None:
                return replay
            evidence_id = self._id(evidence_id, "evidence_id")
            title = self._text(title, "title")
            content_hash = self._text(content_hash, "content_hash", 128)
            resolved = self._resolve_calibers(conn, calibers)
            if conn.execute("SELECT 1 FROM mc_evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
                raise ConflictError("证据包编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_evidence_versions", ctable="mc_evidence",
                    id_col="evidence_id", entity_id=evidence_id,
                    columns=["title", "content_hash", "calibers_json", "producer_actor_id",
                             "created_by", "created_at"],
                    values=[title, content_hash, canonical_json(resolved), actor_id,
                            actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.evidence.registered",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"version": version, "content_hash": content_hash})
                return "evidence", evidence_id, {"evidence_id": evidence_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_evidence",
                                    payload=body, create=create)

    def revise_evidence(self, *, request_id: str, actor_id: str, evidence_id: str, title: str,
                        content_hash: str, calibers: list[dict[str, str | int]]) -> dict[str, Any]:
        body = {"actor_id": actor_id, "evidence_id": evidence_id, "title": title,
                "content_hash": content_hash, "calibers": calibers}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_evidence", payload=body)
            if replay is not None:
                return replay
            evidence_id = self._id(evidence_id, "evidence_id")
            title = self._text(title, "title")
            content_hash = self._text(content_hash, "content_hash", 128)
            resolved = self._resolve_calibers(conn, calibers)
            if not conn.execute("SELECT 1 FROM mc_evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
                raise NotFoundError("证据包不存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_evidence_versions", ctable="mc_evidence",
                    id_col="evidence_id", entity_id=evidence_id,
                    columns=["title", "content_hash", "calibers_json", "producer_actor_id",
                             "created_by", "created_at"],
                    values=[title, content_hash, canonical_json(resolved), actor_id,
                            actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.evidence.revised",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"version": version, "content_hash": content_hash})
                return "evidence", evidence_id, {"evidence_id": evidence_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_evidence",
                                    payload=body, create=create)

    def _resolve_calibers(self, conn, calibers: list[dict[str, str | int]]) -> list[dict[str, Any]]:
        if not isinstance(calibers, list) or not calibers:
            raise ValidationError("证据包至少要声明一个指标口径")
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in calibers:
            if not isinstance(item, dict) or "metric_id" not in item:
                raise ValidationError("口径项必须包含 metric_id")
            metric_id = self._id(str(item["metric_id"]), "metric_id")
            if metric_id in seen:
                raise ValidationError(f"指标口径 {metric_id} 在证据包中重复声明")
            seen.add(metric_id)
            row = conn.execute("SELECT current_version FROM mc_metrics WHERE metric_id=?",
                               (metric_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"指标口径 {metric_id} 不存在")
            version = item.get("metric_version", row["current_version"])
            if not isinstance(version, int) or not (1 <= version <= row["current_version"]):
                raise ValidationError(f"指标口径 {metric_id} 的版本无效")
            resolved.append({"metric_id": metric_id, "metric_version": version})
        return resolved

    # ------------------------------------------------------------------
    # 专家资格
    # ------------------------------------------------------------------
    def register_expert(self, *, request_id: str, actor_id: str, expert_id: str,
                        expert_actor_id: str, display_name: str,
                        qualifications: list[str]) -> dict[str, Any]:
        body = {"actor_id": actor_id, "expert_id": expert_id, "expert_actor_id": expert_actor_id,
                "display_name": display_name, "qualifications": qualifications}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_expert", payload=body)
            if replay is not None:
                return replay
            expert_id = self._id(expert_id, "expert_id")
            expert_actor_id = self._id(expert_actor_id, "expert_actor_id")
            display_name = self._text(display_name, "display_name")
            qualifications = self._qualifications(qualifications)
            target = conn.execute("SELECT * FROM actors WHERE actor_id=?", (expert_actor_id,)).fetchone()
            if target is None:
                raise NotFoundError("专家对应的操作者不存在")
            if conn.execute("SELECT 1 FROM mc_experts WHERE expert_id=? OR actor_id=?",
                            (expert_id, expert_actor_id)).fetchone():
                raise ConflictError("专家资格已经登记")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_expert_versions", ctable="mc_experts",
                    id_col="expert_id", entity_id=expert_id,
                    columns=["actor_id", "display_name", "qualifications_json", "active",
                             "created_by", "created_at"],
                    values=[expert_actor_id, display_name, canonical_json(qualifications), 1,
                            actor_id, self._now()],
                    current_extra={"actor_id": expert_actor_id})
                self._audit(conn, actor_id=actor_id, action="mc.expert.registered",
                            resource_type="expert", resource_id=expert_id,
                            detail={"actor_id": expert_actor_id, "version": version})
                return "expert", expert_id, {"expert_id": expert_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_expert",
                                    payload=body, create=create)

    def revise_expert(self, *, request_id: str, actor_id: str, expert_id: str,
                      display_name: str, qualifications: list[str], active: bool = True) -> dict[str, Any]:
        body = {"actor_id": actor_id, "expert_id": expert_id, "display_name": display_name,
                "qualifications": qualifications, "active": active}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_expert", payload=body)
            if replay is not None:
                return replay
            expert_id = self._id(expert_id, "expert_id")
            display_name = self._text(display_name, "display_name")
            qualifications = self._qualifications(qualifications)
            row = conn.execute("SELECT actor_id FROM mc_experts WHERE expert_id=?", (expert_id,)).fetchone()
            if row is None:
                raise NotFoundError("专家不存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_expert_versions", ctable="mc_experts",
                    id_col="expert_id", entity_id=expert_id,
                    columns=["actor_id", "display_name", "qualifications_json", "active",
                             "created_by", "created_at"],
                    values=[row["actor_id"], display_name, canonical_json(qualifications),
                            1 if active else 0, actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.expert.revised",
                            resource_type="expert", resource_id=expert_id,
                            detail={"version": version, "active": active})
                return "expert", expert_id, {"expert_id": expert_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_expert",
                                    payload=body, create=create)

    def _qualifications(self, qualifications: list[str]) -> list[str]:
        if not isinstance(qualifications, list) or not qualifications:
            raise ValidationError("专家资格项不能为空")
        cleaned = [str(item).strip() for item in qualifications if str(item).strip()]
        if not cleaned:
            raise ValidationError("专家资格项不能为空")
        return cleaned

    def _expert_for_actor(self, conn, actor_id: str):
        row = conn.execute(
            "SELECT e.expert_id, e.current_version, v.active "
            "FROM mc_experts e JOIN mc_expert_versions v "
            "ON e.expert_id=v.expert_id AND e.current_version=v.version WHERE e.actor_id=?",
            (actor_id,)).fetchone()
        if row is None:
            raise PermissionDenied("操作者不具备专家资格")
        if not row["active"]:
            raise PermissionDenied("专家资格已停用")
        return row

    # ------------------------------------------------------------------
    # 里程碑与依赖
    # ------------------------------------------------------------------
    def register_milestone(self, *, request_id: str, actor_id: str, milestone_id: str,
                           project_id: str, name: str, sequence_no: int,
                           topic_id: str | None = None, kind: str = "gate",
                           planned_date: str = "2035-12-31", duration_days: int = 1,
                           required_approvers: int = 1,
                           metric_ids: list[str] | None = None) -> dict[str, Any]:
        metric_ids = metric_ids or []
        body = {"actor_id": actor_id, "milestone_id": milestone_id, "project_id": project_id,
                "topic_id": topic_id, "name": name, "sequence_no": sequence_no, "kind": kind,
                "planned_date": planned_date, "duration_days": duration_days,
                "required_approvers": required_approvers, "metric_ids": metric_ids}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_milestone", payload=body)
            if replay is not None:
                return replay
            milestone_id = self._id(milestone_id, "milestone_id")
            project_id = self._id(project_id, "project_id")
            name = self._text(name, "name")
            planned_date = self._date(planned_date, "planned_date")
            kind = self._validate_kind(kind)
            sequence_no, duration_days, required_approvers = self._milestone_numbers(
                sequence_no, duration_days, required_approvers)
            metrics = self._validate_metric_ids(conn, metric_ids)
            if not conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("专项不存在")
            if topic_id is not None:
                topic_id = self._id(topic_id, "topic_id")
                trow = conn.execute("SELECT * FROM mc_topics WHERE topic_id=? AND project_id=?",
                                    (topic_id, project_id)).fetchone()
                if trow is None:
                    raise NotFoundError("课题不存在或不属于该专项")
            if conn.execute("SELECT 1 FROM mc_milestones WHERE milestone_id=?", (milestone_id,)).fetchone():
                raise ConflictError("里程碑编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_milestone_versions", ctable="mc_milestones",
                    id_col="milestone_id", entity_id=milestone_id,
                    columns=["project_id", "topic_id", "name", "sequence_no", "kind",
                             "planned_date", "duration_days", "required_approvers",
                             "metric_ids_json", "payload_json", "created_by", "created_at"],
                    values=[project_id, topic_id, name, sequence_no, kind, planned_date,
                            duration_days, required_approvers, canonical_json(metrics), "{}",
                            actor_id, self._now()],
                    current_extra={"project_id": project_id, "topic_id": topic_id or ""})
                self._audit(conn, actor_id=actor_id, action="mc.milestone.registered",
                            resource_type="milestone", resource_id=milestone_id,
                            detail={"project_id": project_id, "version": version, "kind": kind})
                return "milestone", milestone_id, {"milestone_id": milestone_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_milestone",
                                    payload=body, create=create)

    def revise_milestone(self, *, request_id: str, actor_id: str, milestone_id: str, name: str,
                         sequence_no: int, planned_date: str, duration_days: int = 1,
                         required_approvers: int = 1, metric_ids: list[str] | None = None,
                         reason: str = "") -> dict[str, Any]:
        """路线调整：发布里程碑新版本，旧版结论保留，沿依赖范围生成后继决定。"""

        metric_ids = metric_ids or []
        body = {"actor_id": actor_id, "milestone_id": milestone_id, "name": name,
                "sequence_no": sequence_no, "planned_date": planned_date,
                "duration_days": duration_days, "required_approvers": required_approvers,
                "metric_ids": metric_ids, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_milestone", payload=body)
            if replay is not None:
                return replay
            milestone_id = self._id(milestone_id, "milestone_id")
            name = self._text(name, "name")
            planned_date = self._date(planned_date, "planned_date")
            sequence_no, duration_days, required_approvers = self._milestone_numbers(
                sequence_no, duration_days, required_approvers)
            metrics = self._validate_metric_ids(conn, metric_ids)
            row = conn.execute(
                "SELECT m.current_version, v.* FROM mc_milestones m JOIN mc_milestone_versions v "
                "ON m.milestone_id=v.milestone_id AND m.current_version=v.version "
                "WHERE m.milestone_id=?", (milestone_id,)).fetchone()
            if row is None:
                raise NotFoundError("里程碑不存在")
            master = conn.execute("SELECT status FROM mc_milestones WHERE milestone_id=?",
                                  (milestone_id,)).fetchone()
            if master["status"] == "terminated":
                raise ImmutableError("已终止的里程碑不能再调整路线")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_milestone_versions", ctable="mc_milestones",
                    id_col="milestone_id", entity_id=milestone_id,
                    columns=["project_id", "topic_id", "name", "sequence_no", "kind",
                             "planned_date", "duration_days", "required_approvers",
                             "metric_ids_json", "payload_json", "created_by", "created_at"],
                    values=[row["project_id"], row["topic_id"], name, sequence_no, row["kind"],
                            planned_date, duration_days, required_approvers, canonical_json(metrics),
                            "{}", actor_id, self._now()])
                change_id = self._propagate_milestone_revision(conn, actor_id=actor_id,
                                                               milestone_id=milestone_id,
                                                               new_version=version, reason=reason)
                self._audit(conn, actor_id=actor_id, action="mc.milestone.revised",
                            resource_type="milestone", resource_id=milestone_id,
                            detail={"version": version, "change_id": change_id})
                return "milestone", milestone_id, {"milestone_id": milestone_id,
                                                   "version": version, "change_id": change_id}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_milestone",
                                    payload=body, create=create)

    def _validate_kind(self, kind: str) -> str:
        if kind not in ("gate", "milestone"):
            raise ValidationError("kind 必须是 gate 或 milestone")
        return kind

    def _milestone_numbers(self, sequence_no: Any, duration_days: Any, required_approvers: Any):
        if not isinstance(sequence_no, int) or sequence_no < 1:
            raise ValidationError("sequence_no 必须是正整数")
        if not isinstance(duration_days, int) or duration_days < 0:
            raise ValidationError("duration_days 必须是非负整数")
        if not isinstance(required_approvers, int) or required_approvers < 1:
            raise ValidationError("required_approvers 必须是正整数")
        return sequence_no, duration_days, required_approvers

    def _validate_metric_ids(self, conn, metric_ids: list[str]) -> list[str]:
        if not isinstance(metric_ids, list):
            raise ValidationError("metric_ids 必须是数组")
        cleaned: list[str] = []
        for item in metric_ids:
            metric_id = self._id(str(item), "metric_id")
            if metric_id in cleaned:
                raise ValidationError(f"指标口径 {metric_id} 重复")
            if not conn.execute("SELECT 1 FROM mc_metrics WHERE metric_id=?", (metric_id,)).fetchone():
                raise NotFoundError(f"指标口径 {metric_id} 不存在")
            cleaned.append(metric_id)
        return cleaned

    def register_dependency(self, *, request_id: str, actor_id: str, dependency_id: str,
                            upstream_milestone_id: str, downstream_milestone_id: str,
                            active: bool = True) -> dict[str, Any]:
        body = {"actor_id": actor_id, "dependency_id": dependency_id,
                "upstream_milestone_id": upstream_milestone_id,
                "downstream_milestone_id": downstream_milestone_id, "active": active}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_dependency", payload=body)
            if replay is not None:
                return replay
            dependency_id = self._id(dependency_id, "dependency_id")
            up = self._id(upstream_milestone_id, "upstream_milestone_id")
            down = self._id(downstream_milestone_id, "downstream_milestone_id")
            if up == down:
                raise ValidationError("里程碑不能依赖自身")
            urow = conn.execute("SELECT * FROM mc_milestones WHERE milestone_id=?", (up,)).fetchone()
            drow = conn.execute("SELECT * FROM mc_milestones WHERE milestone_id=?", (down,)).fetchone()
            if urow is None or drow is None:
                raise NotFoundError("依赖的里程碑不存在")
            uv = conn.execute("SELECT kind,project_id FROM mc_milestone_versions WHERE milestone_id=? "
                              "AND version=?", (up, urow["current_version"])).fetchone()
            dv = conn.execute("SELECT kind,project_id FROM mc_milestone_versions WHERE milestone_id=? "
                              "AND version=?", (down, drow["current_version"])).fetchone()
            if uv["project_id"] != dv["project_id"]:
                raise ValidationError("不能跨专项建立里程碑依赖")
            if uv["kind"] != "gate" or dv["kind"] != "gate":
                raise ValidationError("阶段门依赖只能在两个 gate 之间建立")
            if conn.execute("SELECT 1 FROM mc_dependencies WHERE dependency_id=?",
                            (dependency_id,)).fetchone():
                raise ConflictError("依赖编号已经存在")
            graph, _ = self._load_graph(conn, uv["project_id"])
            if down in graph and up in reachable_downstream(graph, down):
                raise ValidationError("该依赖会形成环")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_dependency_versions", ctable="mc_dependencies",
                    id_col="dependency_id", entity_id=dependency_id,
                    columns=["upstream_milestone_id", "downstream_milestone_id", "active",
                             "created_by", "created_at"],
                    values=[up, down, 1 if active else 0, actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.dependency.registered",
                            resource_type="dependency", resource_id=dependency_id,
                            detail={"upstream": up, "downstream": down, "version": version})
                return "dependency", dependency_id, {"dependency_id": dependency_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.register_dependency",
                                    payload=body, create=create)

    def revise_dependency(self, *, request_id: str, actor_id: str, dependency_id: str,
                          active: bool, reason: str = "") -> dict[str, Any]:
        body = {"actor_id": actor_id, "dependency_id": dependency_id, "active": active,
                "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_dependency", payload=body)
            if replay is not None:
                return replay
            dependency_id = self._id(dependency_id, "dependency_id")
            current = conn.execute(
                "SELECT d.current_version, v.upstream_milestone_id, v.downstream_milestone_id "
                "FROM mc_dependencies d JOIN mc_dependency_versions v "
                "ON d.dependency_id=v.dependency_id AND d.current_version=v.version "
                "WHERE d.dependency_id=?", (dependency_id,)).fetchone()
            if current is None:
                raise NotFoundError("依赖不存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_dependency_versions", ctable="mc_dependencies",
                    id_col="dependency_id", entity_id=dependency_id,
                    columns=["upstream_milestone_id", "downstream_milestone_id", "active",
                             "created_by", "created_at"],
                    values=[current["upstream_milestone_id"], current["downstream_milestone_id"],
                            1 if active else 0, actor_id, self._now()])
                self._audit(conn, actor_id=actor_id, action="mc.dependency.revised",
                            resource_type="dependency", resource_id=dependency_id,
                            detail={"version": version, "active": active})
                return "dependency", dependency_id, {"dependency_id": dependency_id,
                                                     "version": version}

            return self._idempotent(conn, request_id=request_id, action="mc.revise_dependency",
                                    payload=body, create=create)

    def _load_graph(self, conn, project_id: str):
        """返回当前生效依赖的正向/反向邻接表（仅该专项）。"""
        graph: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = {}
        rows = conn.execute(
            "SELECT v.upstream_milestone_id AS up, v.downstream_milestone_id AS down "
            "FROM mc_dependencies d JOIN mc_dependency_versions v "
            "ON d.dependency_id=v.dependency_id AND d.current_version=v.version "
            "JOIN mc_milestones m ON m.milestone_id=v.downstream_milestone_id "
            "WHERE v.active=1 AND m.project_id=?", (project_id,)).fetchall()
        for row in rows:
            graph.setdefault(row["up"], set()).add(row["down"])
            reverse.setdefault(row["down"], set()).add(row["up"])
        return graph, reverse

    # ------------------------------------------------------------------
    # 预算分期与交付承诺
    # ------------------------------------------------------------------
    def register_budget_installment(self, *, request_id: str, actor_id: str, installment_id: str,
                                    project_id: str, topic_id: str, gate_milestone_id: str,
                                    sequence_no: int, amount_cents: int,
                                    currency: str = "CNY") -> dict[str, Any]:
        body = {"actor_id": actor_id, "installment_id": installment_id, "project_id": project_id,
                "topic_id": topic_id, "gate_milestone_id": gate_milestone_id,
                "sequence_no": sequence_no, "amount_cents": amount_cents, "currency": currency}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_budget_installment", payload=body)
            if replay is not None:
                return replay
            installment_id = self._id(installment_id, "installment_id")
            project_id = self._id(project_id, "project_id")
            topic_id = self._id(topic_id, "topic_id")
            gate = self._id(gate_milestone_id, "gate_milestone_id")
            if not isinstance(sequence_no, int) or sequence_no < 1:
                raise ValidationError("sequence_no 必须是正整数")
            amount_cents = self._amount(amount_cents)
            currency = self._text(currency, "currency", 8)
            gate_row = conn.execute(
                "SELECT m.current_version, v.kind, v.project_id FROM mc_milestones m "
                "JOIN mc_milestone_versions v ON m.milestone_id=v.milestone_id "
                "AND m.current_version=v.version WHERE m.milestone_id=?", (gate,)).fetchone()
            if gate_row is None or gate_row["kind"] != "gate":
                raise NotFoundError("预算分期必须绑定一个阶段门")
            if gate_row["project_id"] != project_id:
                raise ValidationError("预算分期的专项与阶段门不一致")
            if not conn.execute("SELECT 1 FROM mc_topics WHERE topic_id=? AND project_id=?",
                                (topic_id, project_id)).fetchone():
                raise NotFoundError("课题不存在或不属于该专项")
            if conn.execute("SELECT 1 FROM mc_budget_installments WHERE installment_id=?",
                            (installment_id,)).fetchone():
                raise ConflictError("预算分期编号已经存在")

            def create():
                conn.execute(
                    "INSERT INTO mc_budget_installments(installment_id,project_id,topic_id,"
                    "gate_milestone_id,sequence_no,amount_cents,currency,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'planned',?)",
                    (installment_id, project_id, topic_id, gate, sequence_no,
                     amount_cents, currency, self._now()))
                self._audit(conn, actor_id=actor_id, action="mc.budget.registered",
                            resource_type="budget_installment", resource_id=installment_id,
                            detail={"gate": gate, "amount_cents": amount_cents})
                return "budget_installment", installment_id, {"installment_id": installment_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.register_budget_installment",
                                    payload=body, create=create)

    def register_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                            wp_id: str, milestone_id: str, title: str, due_date: str,
                            metric_id: str | None = None, target_value: Any = None) -> dict[str, Any]:
        body = {"actor_id": actor_id, "commitment_id": commitment_id, "wp_id": wp_id,
                "milestone_id": milestone_id, "title": title, "due_date": due_date,
                "metric_id": metric_id, "target_value": target_value}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.register_commitment", payload=body)
            if replay is not None:
                return replay
            commitment_id = self._id(commitment_id, "commitment_id")
            wp_id = self._id(wp_id, "wp_id")
            milestone_id = self._id(milestone_id, "milestone_id")
            title = self._text(title, "title")
            due_date = self._date(due_date, "due_date")
            wp = conn.execute("SELECT topic_id FROM mc_workpackages WHERE wp_id=?", (wp_id,)).fetchone()
            if wp is None:
                raise NotFoundError("工作包不存在")
            if not conn.execute("SELECT 1 FROM mc_milestones WHERE milestone_id=?",
                                (milestone_id,)).fetchone():
                raise NotFoundError("里程碑不存在")
            metric_version = None
            if metric_id is not None:
                metric_id = self._id(metric_id, "metric_id")
                mrow = conn.execute("SELECT current_version FROM mc_metrics WHERE metric_id=?",
                                    (metric_id,)).fetchone()
                if mrow is None:
                    raise NotFoundError("指标口径不存在")
                metric_version = mrow["current_version"]
            if conn.execute("SELECT 1 FROM mc_commitments WHERE commitment_id=?",
                            (commitment_id,)).fetchone():
                raise ConflictError("交付承诺编号已经存在")

            def create():
                version = self._publish_version(
                    conn, vtable="mc_commitment_versions", ctable="mc_commitments",
                    id_col="commitment_id", entity_id=commitment_id,
                    columns=["wp_id", "milestone_id", "title", "due_date", "metric_id",
                             "metric_version", "target_value_json", "created_by", "created_at"],
                    values=[wp_id, milestone_id, title, due_date, metric_id, metric_version,
                            canonical_json(target_value), actor_id, self._now()],
                    current_extra={"wp_id": wp_id})
                self._audit(conn, actor_id=actor_id, action="mc.commitment.registered",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"milestone_id": milestone_id, "version": version})
                return "commitment", commitment_id, {"commitment_id": commitment_id, "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.register_commitment", payload=body, create=create)

    def revise_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                          title: str, due_date: str, metric_id: str | None = None,
                          target_value: Any = None) -> dict[str, Any]:
        body = {"actor_id": actor_id, "commitment_id": commitment_id, "title": title,
                "due_date": due_date, "metric_id": metric_id, "target_value": target_value}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.revise_commitment", payload=body)
            if replay is not None:
                return replay
            commitment_id = self._id(commitment_id, "commitment_id")
            title = self._text(title, "title")
            due_date = self._date(due_date, "due_date")
            current = conn.execute(
                "SELECT c.current_version, c.status, v.wp_id, v.milestone_id "
                "FROM mc_commitments c JOIN mc_commitment_versions v "
                "ON c.commitment_id=v.commitment_id AND c.current_version=v.version "
                "WHERE c.commitment_id=?", (commitment_id,)).fetchone()
            if current is None:
                raise NotFoundError("交付承诺不存在")
            if current["status"] == "terminated":
                raise ImmutableError("已终止的承诺不能修订")
            metric_version = None
            if metric_id is not None:
                metric_id = self._id(metric_id, "metric_id")
                mrow = conn.execute("SELECT current_version FROM mc_metrics WHERE metric_id=?",
                                    (metric_id,)).fetchone()
                if mrow is None:
                    raise NotFoundError("指标口径不存在")
                metric_version = mrow["current_version"]

            def create():
                version = self._publish_version(
                    conn, vtable="mc_commitment_versions", ctable="mc_commitments",
                    id_col="commitment_id", entity_id=commitment_id,
                    columns=["wp_id", "milestone_id", "title", "due_date", "metric_id",
                             "metric_version", "target_value_json", "created_by", "created_at"],
                    values=[current["wp_id"], current["milestone_id"], title, due_date, metric_id,
                            metric_version, canonical_json(target_value), actor_id, self._now()])
                conn.execute("UPDATE mc_commitments SET status='active' WHERE commitment_id=?",
                             (commitment_id,))
                # 新版本承诺即视为对“需要重定基线”效果的处置
                conn.execute(
                    "UPDATE mc_change_effects SET status='resolved' "
                    "WHERE target_type='commitment' AND target_id=? AND status='open'",
                    (commitment_id,))
                self._audit(conn, actor_id=actor_id, action="mc.commitment.revised",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"version": version})
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                     "version": version}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.revise_commitment", payload=body, create=create)

    # ------------------------------------------------------------------
    # 阶段门：开启轮次 / 独立验收 / 会签 / 原子通过
    # ------------------------------------------------------------------
    def open_gate_round(self, *, request_id: str, actor_id: str,
                        gate_milestone_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.open_gate_round", payload=body)
            if replay is not None:
                return replay
            gate = self._gate_or_404(conn, gate_milestone_id)

            def create():
                decision_id, round_no = self._create_open_round(conn, gate)
                self._audit(conn, actor_id=actor_id, action="mc.gate.opened",
                            resource_type="gate_decision", resource_id=decision_id,
                            detail={"gate": gate, "round": round_no})
                return "gate_decision", decision_id, {"decision_id": decision_id,
                                                      "gate_milestone_id": gate, "round": round_no}

            return self._idempotent(conn, request_id=request_id, action="mc.open_gate_round",
                                    payload=body, create=create)

    def _gate_or_404(self, conn, gate: str) -> str:
        gate = self._id(gate, "gate_milestone_id")
        row = conn.execute(
            "SELECT m.current_version, m.status, v.kind FROM mc_milestones m "
            "JOIN mc_milestone_versions v ON m.milestone_id=v.milestone_id "
            "AND m.current_version=v.version WHERE m.milestone_id=?", (gate,)).fetchone()
        if row is None:
            raise NotFoundError("里程碑不存在")
        if row["kind"] != "gate":
            raise ValidationError("该里程碑不是阶段门")
        if row["status"] == "terminated":
            raise ImmutableError("阶段门随课题已终止")
        return gate

    def _current_open_round(self, conn, gate: str):
        return conn.execute(
            "SELECT * FROM mc_gate_decisions WHERE gate_milestone_id=? AND state='open' "
            "ORDER BY round DESC LIMIT 1", (gate,)).fetchone()

    def _effective_passed(self, conn, gate: str):
        return conn.execute(
            "SELECT * FROM mc_gate_decisions WHERE gate_milestone_id=? AND state='decided' "
            "AND result='passed' AND status='effective' ORDER BY round DESC LIMIT 1",
            (gate,)).fetchone()

    def _next_round(self, conn, gate: str) -> int:
        row = conn.execute("SELECT MAX(round) AS m FROM mc_gate_decisions WHERE gate_milestone_id=?",
                           (gate,)).fetchone()
        return (row["m"] or 0) + 1

    def _create_open_round(self, conn, gate: str, supersede: bool = False) -> tuple[str, int]:
        """生成后继会签轮次；若旧轮次仍开着则先置为 superseded。"""

        open_row = self._current_open_round(conn, gate)
        if open_row is not None and not supersede:
            raise ConflictError("该阶段门已有进行中的会签轮次")
        if open_row is not None:
            conn.execute(
                "UPDATE mc_gate_decisions SET status='superseded' WHERE decision_id=?",
                (open_row["decision_id"],))
        round_no = self._next_round(conn, gate)
        decision_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO mc_gate_decisions(decision_id,gate_milestone_id,gate_milestone_version,"
            "round,state,status,created_by,created_at) VALUES(?,?,?,?, 'open','effective',?,?)",
            (decision_id, gate, self._milestone_version(conn, gate), round_no,
             "system", self._now()))
        # 后继轮次生成即视为后继决定类效果已落实
        conn.execute(
            "UPDATE mc_change_effects SET status='resolved' WHERE target_type='gate' AND target_id=? "
            "AND effect_type='successor_round' AND status='open'", (gate,))
        return decision_id, round_no

    def _milestone_version(self, conn, gate: str) -> int:
        return conn.execute("SELECT current_version FROM mc_milestones WHERE milestone_id=?",
                            (gate,)).fetchone()["current_version"]

    def submit_acceptance(self, *, request_id: str, actor_id: str, gate_milestone_id: str,
                          result: str, readings: list[dict[str, Any]],
                          note: str = "") -> dict[str, Any]:
        """提交独立验收结论并占用证据。

        readings: [{"metric_id", "evidence_id", "evidence_version"?, "value"?}]
        证据一旦在某次验收中被占用，在撤回释放前不得用于其他验收，
        且证据生产口径必须与指标当前版本兼容。
        """

        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id,
                "result": result, "readings": readings, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            gate = self._gate_or_404(conn, gate_milestone_id)
            if result not in ("passed", "partial", "failed"):
                raise ValidationError("验收结果必须是 passed/partial/failed")
            if not isinstance(readings, list) or not readings:
                raise ValidationError("验收读数不能为空")
            replay = self._replay_or_none(conn, request_id=request_id,
                                          action="mc.submit_acceptance", payload=body)
            if replay is not None:
                return replay
            decision = self._current_open_round(conn, gate)
            if decision is None:
                raise ConflictError("阶段门没有进行中的会签轮次")
            if conn.execute("SELECT 1 FROM mc_acceptances WHERE milestone_id=? AND round=?",
                            (gate, decision["round"])).fetchone():
                raise ConflictError("本轮独立验收已经提交，结论不可更改")
            expert = self._expert_for_actor(conn, actor_id)
            milestone = conn.execute(
                "SELECT * FROM mc_milestone_versions WHERE milestone_id=? AND version=?",
                (gate, decision["gate_milestone_version"])).fetchone()
            required = json.loads(milestone["metric_ids_json"])
            prepared = self._prepare_readings(conn, readings, required, gate, decision["round"])
            basis_material = {
                "gate": gate, "milestone_version": decision["gate_milestone_version"],
                "round": decision["round"], "result": result,
                "readings": [{"metric_id": p["metric_id"], "metric_version": p["metric_version"],
                              "evidence_id": p["evidence_id"], "evidence_version": p["evidence_version"],
                              "value": p["value"]} for p in prepared],
                "lead_expert_id": expert["expert_id"],
                "lead_expert_version": expert["current_version"],
            }
            basis_hash = digest(basis_material)
            acceptance_id = uuid.uuid4().hex
            try:
                conn.execute(
                    "INSERT INTO mc_acceptances(acceptance_id,milestone_id,milestone_version,round,"
                    "result,status,readings_json,lead_expert_id,lead_expert_version,basis_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?, 'effective',?,?,?,?,?,?)",
                    (acceptance_id, gate, decision["gate_milestone_version"], decision["round"],
                     result, canonical_json(basis_material["readings"]), expert["expert_id"],
                     expert["current_version"], basis_hash, actor_id, self._now()))
                # 一份证据版本在一次资金释放验收中只产生一条占用；多口径证据
                # 可在同一验收内支撑多个指标，但不能再被其他验收重复占用
                occupied: set[tuple[str, int]] = set()
                for p in prepared:
                    key = (p["evidence_id"], p["evidence_version"])
                    if key in occupied:
                        continue
                    occupied.add(key)
                    conn.execute(
                        "INSERT INTO mc_evidence_usages(usage_id,evidence_id,evidence_version,"
                        "scope_type,scope_id,status,created_at) "
                        "VALUES(?,?,?, 'gate_acceptance',?, 'consumed',?)",
                        (uuid.uuid4().hex, p["evidence_id"], p["evidence_version"],
                         acceptance_id, self._now()))
            except Exception as exc:
                raise ConflictError("证据已被其他验收占用，不能跨口径重复使用") from exc
            conn.execute("UPDATE mc_gate_decisions SET acceptance_id=? WHERE decision_id=?",
                         (acceptance_id, decision["decision_id"]))
            self._audit(conn, actor_id=actor_id, action="mc.acceptance.submitted",
                        resource_type="acceptance", resource_id=acceptance_id,
                        detail={"gate": gate, "round": decision["round"], "result": result,
                                "basis_hash": basis_hash})
            return self._record_receipt(
                conn, request_id=request_id, action="mc.submit_acceptance", payload=body,
                resource_type="acceptance", resource_id=acceptance_id,
                response={"acceptance_id": acceptance_id, "gate_milestone_id": gate,
                          "round": decision["round"], "result": result, "basis_hash": basis_hash})

    def _prepare_readings(self, conn, readings, required, gate, round_no):
        by_metric: dict[str, dict[str, Any]] = {}
        for item in readings:
            if not isinstance(item, dict) or "metric_id" not in item or "evidence_id" not in item:
                raise ValidationError("每条读数必须包含 metric_id 与 evidence_id")
            metric_id = self._id(str(item["metric_id"]), "metric_id")
            evidence_id = self._id(str(item["evidence_id"]), "evidence_id")
            if metric_id in by_metric:
                raise ValidationError(f"指标 {metric_id} 的读数重复")
            erow = conn.execute("SELECT current_version FROM mc_evidence WHERE evidence_id=?",
                                (evidence_id,)).fetchone()
            if erow is None:
                raise NotFoundError(f"证据包 {evidence_id} 不存在")
            evidence_version = item.get("evidence_version", erow["current_version"])
            if not isinstance(evidence_version, int) or not (1 <= evidence_version <= erow["current_version"]):
                raise ValidationError(f"证据包 {evidence_id} 的版本无效")
            ev = conn.execute("SELECT * FROM mc_evidence_versions WHERE evidence_id=? AND version=?",
                              (evidence_id, evidence_version)).fetchone()
            calibers = json.loads(ev["calibers_json"])
            caliber = next((c for c in calibers if c["metric_id"] == metric_id), None)
            if caliber is None:
                raise ValidationError(f"证据 {evidence_id} 未声明指标 {metric_id} 的口径")
            history = [dict(r) for r in conn.execute(
                "SELECT version,compatible FROM mc_metric_versions WHERE metric_id=? ORDER BY version",
                (metric_id,)).fetchall()]
            current_version = history[-1]["version"]
            if not caliber_compatible(history, caliber["metric_version"], current_version):
                raise ValidationError(
                    f"证据 {evidence_id} 基于指标 {metric_id} 的旧口径 v{caliber['metric_version']}，"
                    f"当前为 v{current_version}，口径不兼容，不能用于本次验收")
            occupant = conn.execute(
                "SELECT scope_id FROM mc_evidence_usages WHERE evidence_id=? AND evidence_version=? "
                "AND status='consumed'", (evidence_id, evidence_version)).fetchone()
            if occupant is not None:
                raise ConflictError(
                    f"证据 {evidence_id} 已被验收 {occupant['scope_id']} 占用，不能重复申请资金释放")
            by_metric[metric_id] = {"metric_id": metric_id, "metric_version": current_version,
                                    "evidence_id": evidence_id,
                                    "evidence_version": evidence_version,
                                    "value": item.get("value")}
        missing = [metric_id for metric_id in required if metric_id not in by_metric]
        if missing:
            raise ValidationError(f"阶段门要求的指标缺少验收读数：{', '.join(missing)}")
        return [by_metric[m] for m in required]

    def sign_gate(self, *, request_id: str, actor_id: str, gate_milestone_id: str,
                  opinion: str, note: str = "") -> dict[str, Any]:
        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id,
                "opinion": opinion, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.sign_gate", payload=body)
            if replay is not None:
                return replay
            gate = self._gate_or_404(conn, gate_milestone_id)
            if opinion not in ("approved", "rejected", "abstain"):
                raise ValidationError("会签意见必须是 approved/rejected/abstain")
            decision = self._current_open_round(conn, gate)
            if decision is None:
                raise ConflictError("阶段门没有进行中的会签轮次")
            expert = self._expert_for_actor(conn, actor_id)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO mc_gate_signoffs(decision_id,expert_id,expert_version,opinion,"
                        "note,status,signed_by,signed_at) VALUES(?,?,?,?,?, 'active',?,?)",
                        (decision["decision_id"], expert["expert_id"], expert["current_version"],
                         opinion, note, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("专家已在本轮会签，重复会签被拒绝") from exc
                self._audit(conn, actor_id=actor_id, action="mc.gate.signed",
                            resource_type="gate_decision", resource_id=decision["decision_id"],
                            detail={"gate": gate, "expert_id": expert["expert_id"],
                                    "opinion": opinion})
                return "gate_signoff", decision["decision_id"], {
                    "decision_id": decision["decision_id"], "expert_id": expert["expert_id"],
                    "opinion": opinion}

            return self._idempotent(conn, request_id=request_id, action="mc.sign_gate",
                                    payload=body, create=create)

    def withdraw_signoff(self, *, request_id: str, actor_id: str,
                         gate_milestone_id: str, reason: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer", "admin")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.withdraw_signoff", payload=body)
            if replay is not None:
                return replay
            gate = self._gate_or_404(conn, gate_milestone_id)
            decision = self._current_open_round(conn, gate)
            if decision is None:
                raise ConflictError("阶段门没有进行中的会签轮次")
            expert = self._expert_for_actor(conn, actor_id)
            row = conn.execute(
                "SELECT * FROM mc_gate_signoffs WHERE decision_id=? AND expert_id=?",
                (decision["decision_id"], expert["expert_id"])).fetchone()
            if row is None or row["status"] != "active":
                raise NotFoundError("没有可撤回的有效会签意见")

            def create():
                conn.execute(
                    "UPDATE mc_gate_signoffs SET status='withdrawn', withdrawn_at=? "
                    "WHERE decision_id=? AND expert_id=?",
                    (self._now(), decision["decision_id"], expert["expert_id"]))
                self._audit(conn, actor_id=actor_id, action="mc.gate.signoff_withdrawn",
                            resource_type="gate_decision", resource_id=decision["decision_id"],
                            detail={"gate": gate, "expert_id": expert["expert_id"],
                                    "reason": reason})
                return "gate_signoff", decision["decision_id"],
                {"decision_id": decision["decision_id"], "withdrawn": True}

            return self._idempotent(conn, request_id=request_id, action="mc.withdraw_signoff",
                                    payload=body, create=create)

    def decide_gate(self, *, request_id: str, actor_id: str,
                    gate_milestone_id: str) -> dict[str, Any]:
        """原子地核算阶段门：前置里程碑与独立验收同时有效才通过并释放额度。"""

        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            gate = self._gate_or_404(conn, gate_milestone_id)
            replay = self._replay_or_none(conn, request_id=request_id, action="mc.decide_gate",
                                          payload=body)
            if replay is not None:
                return replay
            decision = self._current_open_round(conn, gate)
            if decision is None:
                raise ConflictError("阶段门没有进行中的会签轮次")

            acceptance = conn.execute(
                "SELECT * FROM mc_acceptances WHERE acceptance_id=?",
                (decision["acceptance_id"],)).fetchone() if decision["acceptance_id"] else None

            # partial / failed：形成本轮结论并沿依赖生成后继事项，但不释放资金
            if acceptance is not None and acceptance["status"] == "effective" \
                    and acceptance["result"] in ("partial", "failed"):
                result = "partial" if acceptance["result"] == "partial" else "rejected"
                conn.execute(
                    "UPDATE mc_gate_decisions SET state='decided', result=?, decided_at=? "
                    "WHERE decision_id=?", (result, self._now(), decision["decision_id"]))
                change_id = None
                successor_round = None
                if result == "partial":
                    change_id = self._open_change(
                        conn, actor_id=actor_id, change_type="partial_pass",
                        origin_type="gate_decision", origin_id=decision["decision_id"],
                        reason="独立验收部分通过，需整改后进入后继轮次",
                        payload={"gate": gate, "round": decision["round"]},
                        effects=self._gate_failure_effects(conn, gate))
                    _, successor_round = self._create_open_round(conn, gate, supersede=False)
                self._audit(conn, actor_id=actor_id, action="mc.gate.decided",
                            resource_type="gate_decision", resource_id=decision["decision_id"],
                            detail={"gate": gate, "result": result, "change_id": change_id})
                return self._record_receipt(
                    conn, request_id=request_id, action="mc.decide_gate", payload=body,
                    resource_type="gate_decision", resource_id=decision["decision_id"],
                    response={"decision_id": decision["decision_id"], "gate_milestone_id": gate,
                              "result": result, "released_cents": 0, "change_id": change_id,
                              "successor_round": successor_round})

            reasons = self._block_reasons(conn, gate, decision, acceptance)
            if reasons:
                raise GateBlocked(reasons)

            # —— 原子通过 ——
            approvals = self._active_approvals(conn, decision["decision_id"])
            installments = conn.execute(
                "SELECT installment_id FROM mc_budget_installments WHERE gate_milestone_id=? "
                "AND status='planned'", (gate,)).fetchall()
            released = self._release_installments(conn, gate, decision, acceptance)
            # 后继轮次再次通过：旧的生效通过决定留痕并让位（唯一生效通过约束）
            conn.execute(
                "UPDATE mc_gate_decisions SET status='superseded' WHERE gate_milestone_id=? "
                "AND state='decided' AND result='passed' AND status='effective' "
                "AND decision_id<>?", (gate, decision["decision_id"]))
            conn.execute(
                "UPDATE mc_gate_decisions SET state='decided', result='passed', status='effective', "
                "basis_hash=?, decided_at=? WHERE decision_id=?",
                (acceptance["basis_hash"], self._now(), decision["decision_id"]))
            # 该门上挂着的阻断/缓办效果随通过消解；该门恢复后解除其造成的下游挂起
            conn.execute(
                "UPDATE mc_change_effects SET status='resolved' WHERE target_type='gate' "
                "AND target_id=? AND status='open'", (gate,))
            self._clear_downstream_holds(conn, gate)
            self._audit(conn, actor_id=actor_id, action="mc.gate.decided",
                        resource_type="gate_decision", resource_id=decision["decision_id"],
                        detail={"gate": gate, "result": "passed", "round": decision["round"],
                                "released_cents": released,
                                "installments": [r["installment_id"] for r in installments],
                                "approvers": sorted(a["expert_id"] for a in approvals)})
            return self._record_receipt(
                conn, request_id=request_id, action="mc.decide_gate", payload=body,
                resource_type="gate_decision", resource_id=decision["decision_id"],
                response={"decision_id": decision["decision_id"], "gate_milestone_id": gate,
                          "result": "passed", "round": decision["round"],
                          "released_cents": released})

    def _gate_failure_effects(self, conn, gate: str) -> list[tuple[str, str, str, dict]]:
        project_id = conn.execute("SELECT project_id FROM mc_milestones WHERE milestone_id=?",
                                  (gate,)).fetchone()["project_id"]
        graph, _ = self._load_graph(conn, project_id)
        effects: list[tuple[str, str, str, dict]] = [
            ("gate", gate, "successor_round", {"auto": True})]
        for down in sorted(reachable_downstream(graph, gate)):
            effects.append(("gate", down, "hold_downstream", {"upstream": gate}))
            if conn.execute("SELECT 1 FROM mc_budget_installments WHERE gate_milestone_id=? "
                            "AND status='planned'", (down,)).fetchone():
                effects.append(("gate", down, "block_funds", {"upstream": gate}))
        return effects

    def _active_approvals(self, conn, decision_id: str):
        """有效赞成票：会签未撤回，且专家当前资格仍然有效。

        专家资格在签署之后被停用/撤回，其会签立即失效，不计入法定人数；
        签署时的资格版本快照仍保留在 mc_gate_signoffs 中供审计。
        """

        rows = conn.execute(
            "SELECT s.* FROM mc_gate_signoffs s "
            "JOIN mc_experts e ON s.expert_id=e.expert_id "
            "JOIN mc_expert_versions v ON e.expert_id=v.expert_id "
            "AND e.current_version=v.version "
            "WHERE s.decision_id=? AND s.status='active' AND s.opinion='approved' AND v.active=1",
            (decision_id,)).fetchall()
        return rows

    def _block_reasons(self, conn, gate: str, decision, acceptance) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        milestone = conn.execute(
            "SELECT m.status AS mstatus FROM mc_milestones m WHERE m.milestone_id=?",
            (gate,)).fetchone()
        topic_row = conn.execute(
            "SELECT t.status FROM mc_milestones m LEFT JOIN mc_topics t ON m.topic_id=t.topic_id "
            "WHERE m.milestone_id=?", (gate,)).fetchone()
        if milestone["mstatus"] == "terminated" or (topic_row is not None and topic_row["status"] == "terminated"):
            reasons.append({"code": "topic_terminated", "gate": gate,
                            "message": "所属课题已终止"})
        if decision["gate_milestone_version"] != self._milestone_version(conn, gate):
            reasons.append({"code": "milestone_version_stale", "gate": gate,
                            "round_version": decision["gate_milestone_version"],
                            "current_version": self._milestone_version(conn, gate),
                            "message": "路线已调整，本会签轮次基于旧版里程碑，需进入后继轮次"})
        # 独立验收
        if acceptance is None:
            reasons.append({"code": "acceptance_missing", "gate": gate,
                            "message": "独立验收结论尚未提交"})
        elif acceptance["status"] != "effective":
            reasons.append({"code": "acceptance_withdrawn", "gate": gate,
                            "acceptance_id": acceptance["acceptance_id"],
                            "message": "独立验收结论已被撤回"})
        else:
            readings = json.loads(acceptance["readings_json"])
            for reading in readings:
                history = [dict(r) for r in conn.execute(
                    "SELECT version,compatible FROM mc_metric_versions WHERE metric_id=? ORDER BY version",
                    (reading["metric_id"],)).fetchall()]
                if history and not caliber_compatible(history, reading["metric_version"],
                                                      history[-1]["version"]):
                    reasons.append({"code": "caliber_incompatible", "gate": gate,
                                    "metric_id": reading["metric_id"],
                                    "used_version": reading["metric_version"],
                                    "current_version": history[-1]["version"],
                                    "message": "验收采用的指标口径已发生不兼容变更"})
                occupant = conn.execute(
                    "SELECT status FROM mc_evidence_usages WHERE scope_id=? AND evidence_id=? "
                    "AND evidence_version=?",
                    (acceptance["acceptance_id"], reading["evidence_id"],
                     reading["evidence_version"])).fetchone()
                if occupant is None or occupant["status"] != "consumed":
                    reasons.append({"code": "evidence_released", "gate": gate,
                                    "evidence_id": reading["evidence_id"],
                                    "message": "验收证据占用已被释放，结论依据不再成立"})
        # 前置里程碑
        project_id = conn.execute("SELECT project_id FROM mc_milestones WHERE milestone_id=?",
                                  (gate,)).fetchone()["project_id"]
        _, reverse = self._load_graph(conn, project_id)
        for up in sorted(reverse.get(gate, set())):
            passed = self._effective_passed(conn, up)
            up_status = conn.execute("SELECT status FROM mc_milestones WHERE milestone_id=?",
                                     (up,)).fetchone()["status"]
            if up_status == "terminated":
                reasons.append({"code": "upstream_terminated", "gate": gate, "upstream": up,
                                "message": "前置里程碑随课题终止，无法满足"})
            elif passed is None:
                reasons.append({"code": "prerequisite_pending", "gate": gate, "upstream": up,
                                "message": "前置阶段门尚未有效通过"})
        # 会签法定人数（含独立性：验收责任专家不能计入赞成）
        required = conn.execute(
            "SELECT required_approvers FROM mc_milestone_versions WHERE milestone_id=? AND version=?",
            (gate, decision["gate_milestone_version"])).fetchone()["required_approvers"]
        approvals = self._active_approvals(conn, decision["decision_id"])
        if acceptance is not None and acceptance["status"] == "effective":
            approvals = [a for a in approvals
                         if a["expert_id"] != acceptance["lead_expert_id"]]
        if len(approvals) < required:
            reasons.append({"code": "approvals_below_quorum", "gate": gate,
                            "approved": len(approvals), "required": required,
                            "message": "有效赞成票未达到法定人数（验收责任专家不计入）"})
        # 未处置的变更效果：路线调整/整改/撤回/课题终止沿依赖挂账的阻断
        open_effects = conn.execute(
            "SELECT e.effect_type, e.detail_json FROM mc_change_effects e "
            "WHERE e.target_type='gate' AND e.target_id=? AND e.status='open' "
            "AND e.effect_type IN ('block_funds','hold_downstream') ORDER BY e.sequence_no",
            (gate,)).fetchall()
        seen_codes: set[str] = set()
        for effect in open_effects:
            code = effect["effect_type"]
            if code in seen_codes:
                continue
            seen_codes.add(code)
            if code == "block_funds":
                reasons.append({"code": "change_blocks_funds", "gate": gate,
                                "detail": json.loads(effect["detail_json"]),
                                "message": "存在沿依赖传播且未处置的变更，资金暂缓释放"})
            else:
                reasons.append({"code": "downstream_held", "gate": gate,
                                "detail": json.loads(effect["detail_json"]),
                                "message": "上游处于整改/撤回/终止处置中，下游阶段门被挂起"})
        return reasons

    def _release_installments(self, conn, gate, decision, acceptance) -> int:
        total = 0
        rows = conn.execute(
            "SELECT * FROM mc_budget_installments WHERE gate_milestone_id=? AND status='planned'",
            (gate,)).fetchall()
        for row in rows:
            ledger_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO mc_budget_ledger(ledger_id,installment_id,amount_cents,"
                "gate_milestone_id,gate_milestone_version,decision_id,acceptance_id,status,"
                "released_at) VALUES(?,?,?,?,?,?,?, 'released',?)",
                (ledger_id, row["installment_id"], row["amount_cents"], gate,
                 decision["gate_milestone_version"], decision["decision_id"],
                 acceptance["acceptance_id"], self._now()))
            conn.execute("UPDATE mc_budget_installments SET status='released' WHERE installment_id=?",
                         (row["installment_id"],))
            total += row["amount_cents"]
        return total

    # ------------------------------------------------------------------
    # 限期整改 / 结论撤回 / 课题终止
    # ------------------------------------------------------------------
    def issue_rectification(self, *, request_id: str, actor_id: str, gate_milestone_id: str,
                            due_date: str, reason: str) -> dict[str, Any]:
        """下发限期整改并生成后继会签轮次。"""

        body = {"actor_id": actor_id, "gate_milestone_id": gate_milestone_id,
                "due_date": due_date, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.issue_rectification", payload=body)
            if replay is not None:
                return replay
            gate = self._gate_or_404(conn, gate_milestone_id)
            due_date = self._date(due_date, "due_date")
            reason = self._text(reason, "reason")
            open_row = self._current_open_round(conn, gate)
            latest = conn.execute(
                "SELECT * FROM mc_gate_decisions WHERE gate_milestone_id=? ORDER BY round DESC LIMIT 1",
                (gate,)).fetchone()
            if latest is None:
                raise ConflictError("阶段门尚未开启过会签轮次")

            def create():
                decision_id, round_no = self._create_open_round(conn, gate, supersede=True)
                change_id = self._open_change(
                    conn, actor_id=actor_id, change_type="rectification",
                    origin_type="gate_decision", origin_id=decision_id,
                    reason=reason, payload={"gate": gate, "due_date": due_date},
                    effects=[("gate", gate, "successor_round", {"due_date": due_date, "auto": True}),
                             *self._downstream_hold_effects(conn, gate)])
                self._resolve_successor_effect(conn, gate)
                self._audit(conn, actor_id=actor_id, action="mc.rectification.issued",
                            resource_type="change_order", resource_id=change_id,
                            detail={"gate": gate, "round": round_no, "due_date": due_date})
                return "change_order", change_id, {"change_id": change_id,
                                                   "decision_id": decision_id, "round": round_no}

            return self._idempotent(conn, request_id=request_id, action="mc.issue_rectification",
                                    payload=body, create=create)

    def _downstream_hold_effects(self, conn, gate: str) -> list[tuple[str, str, str, dict]]:
        project_id = conn.execute("SELECT project_id FROM mc_milestones WHERE milestone_id=?",
                                  (gate,)).fetchone()["project_id"]
        graph, _ = self._load_graph(conn, project_id)
        effects: list[tuple[str, str, str, dict]] = []
        for down in sorted(reachable_downstream(graph, gate)):
            effects.append(("gate", down, "hold_downstream", {"upstream": gate}))
            if conn.execute("SELECT 1 FROM mc_budget_installments WHERE gate_milestone_id=? "
                            "AND status='planned'", (down,)).fetchone():
                effects.append(("gate", down, "block_funds", {"upstream": gate}))
        return effects

    def withdraw_acceptance(self, *, request_id: str, actor_id: str, acceptance_id: str,
                            reason: str) -> dict[str, Any]:
        """撤回专家结论：释放证据占用、撤销未支付额度，并沿依赖生成后继决定。"""

        body = {"actor_id": actor_id, "acceptance_id": acceptance_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "reviewer")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.withdraw_acceptance", payload=body)
            if replay is not None:
                return replay
            acceptance_id = self._id(acceptance_id, "acceptance_id")
            acceptance = conn.execute("SELECT * FROM mc_acceptances WHERE acceptance_id=?",
                                      (acceptance_id,)).fetchone()
            if acceptance is None:
                raise NotFoundError("验收结论不存在")
            if acceptance["status"] != "effective":
                raise ImmutableError("验收结论已被撤回，不能重复撤回")
            gate = acceptance["milestone_id"]
            if actor["role"] == "reviewer" and actor["actor_id"] != self._lead_actor(conn, acceptance):
                raise PermissionDenied("只能由结论责任专家或管理员撤回")
            reason = self._text(reason, "reason")

            def create():
                conn.execute(
                    "UPDATE mc_acceptances SET status='withdrawn', withdrawn_at=? WHERE acceptance_id=?",
                    (self._now(), acceptance_id))
                conn.execute(
                    "UPDATE mc_evidence_usages SET status='released' WHERE scope_id=? "
                    "AND status='consumed'", (acceptance_id,))
                decision = conn.execute(
                    "SELECT * FROM mc_gate_decisions WHERE acceptance_id=? AND state='decided'",
                    (acceptance_id,)).fetchone()
                revoked_cents = 0
                if decision is not None and decision["result"] == "passed":
                    conn.execute(
                        "UPDATE mc_gate_decisions SET status='withdrawn_basis' WHERE decision_id=?",
                        (decision["decision_id"],))
                    for ledger in conn.execute(
                            "SELECT * FROM mc_budget_ledger WHERE decision_id=? AND status='released'",
                            (decision["decision_id"],)).fetchall():
                        conn.execute("UPDATE mc_budget_ledger SET status='revoked' WHERE ledger_id=?",
                                     (ledger["ledger_id"],))
                        conn.execute(
                            "UPDATE mc_budget_installments SET status='planned' WHERE installment_id=?",
                            (ledger["installment_id"],))
                        revoked_cents += ledger["amount_cents"]
                    # 已支付/已关账台账保持原样，旧版结论仍可审计
                successor_id, round_no = self._create_open_round(conn, gate, supersede=True)
                effects = [("gate", gate, "successor_round", {"auto": True}),
                           ("gate", gate, "block_funds", {})]
                effects.extend(self._downstream_hold_effects(conn, gate))
                change_id = self._open_change(
                    conn, actor_id=actor_id, change_type="conclusion_withdrawn",
                    origin_type="acceptance", origin_id=acceptance_id, reason=reason,
                    payload={"gate": gate, "revoked_cents": revoked_cents,
                             "successor_round": round_no}, effects=effects)
                self._resolve_successor_effect(conn, gate)
                self._audit(conn, actor_id=actor_id, action="mc.acceptance.withdrawn",
                            resource_type="acceptance", resource_id=acceptance_id,
                            detail={"gate": gate, "revoked_cents": revoked_cents,
                                    "change_id": change_id, "successor_round": round_no})
                return "change_order", change_id, {"change_id": change_id, "gate": gate,
                                                   "revoked_cents": revoked_cents,
                                                   "successor_round": round_no}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.withdraw_acceptance", payload=body, create=create)

    def _lead_actor(self, conn, acceptance) -> str:
        row = conn.execute("SELECT actor_id FROM mc_experts WHERE expert_id=?",
                           (acceptance["lead_expert_id"],)).fetchone()
        return row["actor_id"] if row else ""

    def terminate_topic(self, *, request_id: str, actor_id: str, topic_id: str,
                        reason: str) -> dict[str, Any]:
        """课题终止：冻结未释放额度与在途轮次，并沿下游依赖生成阻断效果。"""

        body = {"actor_id": actor_id, "topic_id": topic_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.terminate_topic", payload=body)
            if replay is not None:
                return replay
            topic_id = self._id(topic_id, "topic_id")
            topic = conn.execute("SELECT * FROM mc_topics WHERE topic_id=?", (topic_id,)).fetchone()
            if topic is None:
                raise NotFoundError("课题不存在")
            if topic["status"] == "terminated":
                raise ImmutableError("课题已经终止")
            reason = self._text(reason, "reason")

            def create():
                now = self._now()
                conn.execute("UPDATE mc_topics SET status='terminated', terminated_at=? WHERE topic_id=?",
                             (now, topic_id))
                effects: list[tuple[str, str, str, dict]] = [
                    ("topic", topic_id, "terminate", {})]
                gates = conn.execute(
                    "SELECT milestone_id FROM mc_milestones WHERE topic_id=?", (topic_id,)).fetchall()
                downstream: set[str] = set()
                for grow in gates:
                    gate = grow["milestone_id"]
                    conn.execute("UPDATE mc_milestones SET status='terminated' WHERE milestone_id=?",
                                 (gate,))
                    for decision in conn.execute(
                            "SELECT * FROM mc_gate_decisions WHERE gate_milestone_id=? AND state='open'",
                            (gate,)).fetchall():
                        conn.execute(
                            "UPDATE mc_gate_decisions SET state='decided', result='terminated', "
                            "decided_at=? WHERE decision_id=?", (now, decision["decision_id"]))
                    for installment in conn.execute(
                            "SELECT * FROM mc_budget_installments WHERE topic_id=? AND status='planned'",
                            (topic_id,)).fetchall():
                        conn.execute("UPDATE mc_budget_installments SET status='void' WHERE installment_id=?",
                                     (installment["installment_id"],))
                        effects.append(("budget", installment["installment_id"], "void_installment",
                                        {"amount_cents": installment["amount_cents"]}))
                    for commitment in conn.execute(
                            "SELECT c.commitment_id FROM mc_commitments c JOIN mc_commitment_versions v "
                            "ON c.commitment_id=v.commitment_id AND c.current_version=v.version "
                            "JOIN mc_workpackages w ON v.wp_id=w.wp_id WHERE w.topic_id=? "
                            "AND c.status='active'", (topic_id,)).fetchall():
                        conn.execute("UPDATE mc_commitments SET status='terminated' WHERE commitment_id=?",
                                     (commitment["commitment_id"],))
                        effects.append(("commitment", commitment["commitment_id"], "terminate", {}))
                    project_id = conn.execute("SELECT project_id FROM mc_milestones WHERE milestone_id=?",
                                              (gate,)).fetchone()["project_id"]
                    graph, _ = self._load_graph(conn, project_id)
                    downstream |= reachable_downstream(graph, gate)
                for down in sorted(downstream):
                    effects.append(("gate", down, "hold_downstream", {"topic_id": topic_id}))
                    row = conn.execute("SELECT 1 FROM mc_budget_installments WHERE gate_milestone_id=? "
                                       "AND status='planned'", (down,)).fetchone()
                    if row:
                        effects.append(("gate", down, "block_funds", {"topic_id": topic_id}))
                change_id = self._open_change(
                    conn, actor_id=actor_id, change_type="topic_termination",
                    origin_type="topic", origin_id=topic_id, reason=reason,
                    payload={"topic_id": topic_id}, effects=effects)
                self._audit(conn, actor_id=actor_id, action="mc.topic.terminated",
                            resource_type="topic", resource_id=topic_id,
                            detail={"change_id": change_id, "gates": len(gates)})
                return "change_order", change_id, {"change_id": change_id, "topic_id": topic_id}

            return self._idempotent(conn, request_id=request_id, action="mc.terminate_topic",
                                    payload=body, create=create)

    # ------------------------------------------------------------------
    # 变更单与传播
    # ------------------------------------------------------------------
    def _open_change(self, conn, *, actor_id: str, change_type: str, origin_type: str,
                     origin_id: str, reason: str, payload: dict[str, Any],
                     effects: list[tuple[str, str, str, dict]]) -> str:
        change_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO mc_change_orders(change_id,change_type,origin_type,origin_id,reason,"
            "status,payload_json,created_by,created_at,effective_at) VALUES(?,?,?,?,?, 'effective',?,?,?,?)",
            (change_id, change_type, origin_type, origin_id, reason, canonical_json(payload),
             actor_id, self._now(), self._now()))
        seq = 0
        seen: set[tuple[str, str, str]] = set()
        for target_type, target_id, effect_type, detail in effects:
            key = (target_type, target_id, effect_type)
            if key in seen:
                continue
            seen.add(key)
            seq += 1
            conn.execute(
                "INSERT INTO mc_change_effects(effect_id,change_id,sequence_no,target_type,"
                "target_id,effect_type,status,detail_json,created_at) VALUES(?,?,?,?,?,?, 'open',?,?)",
                (uuid.uuid4().hex, change_id, seq, target_type, target_id, effect_type,
                 canonical_json(detail), self._now()))
        return change_id

    def _resolve_successor_effect(self, conn, gate: str) -> None:
        """后继会签轮次已建立时，结清该门挂起的 successor_round 效果。"""

        conn.execute(
            "UPDATE mc_change_effects SET status='resolved' WHERE status='open' "
            "AND target_type='gate' AND target_id=? AND effect_type='successor_round'",
            (gate,))

    def _clear_downstream_holds(self, conn, gate: str) -> None:
        """上游门重新有效通过后，解除以其为直接来源的下游挂起/阻断效果。

        仅清理 detail.upstream 精确等于该门的挂账；口径（metric_id）或
        课题终止（topic_id）引发的挂起不在此列，须由对应变化恢复路径处置。
        """

        conn.execute(
            "UPDATE mc_change_effects SET status='resolved' WHERE status='open' "
            "AND target_type='gate' AND effect_type IN ('hold_downstream','block_funds') "
            "AND json_extract(detail_json,'$.upstream')=?", (gate,))

    def _propagate_milestone_revision(self, conn, *, actor_id: str, milestone_id: str,
                                      new_version: int, reason: str) -> str:
        effects: list[tuple[str, str, str, dict]] = []
        open_round = self._current_open_round(conn, milestone_id)
        if open_round is not None:
            effects.append(("gate", milestone_id, "successor_round", {"new_version": new_version}))
        effects.extend(self._downstream_hold_effects(conn, milestone_id))
        for crow in conn.execute(
                "SELECT commitment_id FROM mc_commitment_versions WHERE milestone_id=? "
                "AND commitment_id IN (SELECT commitment_id FROM mc_commitments WHERE status='active')",
                (milestone_id,)).fetchall():
            effects.append(("commitment", crow["commitment_id"], "require_rebaseline",
                            {"new_version": new_version}))
        change_id = self._open_change(
            conn, actor_id=actor_id, change_type="route_adjustment",
            origin_type="milestone", origin_id=f"{milestone_id}#v{new_version}",
            reason=reason or "里程碑路线调整",
            payload={"milestone_id": milestone_id, "new_version": new_version}, effects=effects)
        # 变更效果先落库再开后继轮次，由 _create_open_round 将 successor_round 置为 resolved
        if open_round is not None:
            self._create_open_round(conn, milestone_id, supersede=True)
        return change_id

    def _propagate_metric_break(self, conn, *, actor_id: str, metric_id: str,
                                new_version: int, reason: str) -> str:
        effects: list[tuple[str, str, str, dict]] = []
        for crow in conn.execute(
                "SELECT v.commitment_id FROM mc_commitment_versions v "
                "JOIN mc_commitments c ON v.commitment_id=c.commitment_id "
                "AND v.version=c.current_version "
                "WHERE v.metric_id=? AND c.status='active'",
                (metric_id,)).fetchall():
            effects.append(("commitment", crow["commitment_id"], "require_rebaseline",
                            {"metric_id": metric_id, "new_version": new_version}))
        affected_gates: set[str] = set()
        for acc in conn.execute(
                "SELECT DISTINCT milestone_id FROM mc_acceptances WHERE readings_json LIKE ?",
                (f'%"{metric_id}"%',)).fetchall():
            affected_gates.add(acc["milestone_id"])
        expanded: set[str] = set(affected_gates)
        for gate in list(affected_gates):
            project_id = conn.execute("SELECT project_id FROM mc_milestones WHERE milestone_id=?",
                                      (gate,)).fetchone()["project_id"]
            graph, _ = self._load_graph(conn, project_id)
            expanded |= reachable_downstream(graph, gate)
        for gate in sorted(expanded):
            passed = self._effective_passed(conn, gate)
            if passed is None:
                effects.append(("gate", gate, "hold_downstream", {"metric_id": metric_id}))
                if conn.execute("SELECT 1 FROM mc_budget_installments WHERE gate_milestone_id=? "
                                "AND status='planned'", (gate,)).fetchone():
                    effects.append(("gate", gate, "block_funds", {"metric_id": metric_id}))
            else:
                effects.append(("gate", gate, "require_rebaseline",
                                {"metric_id": metric_id, "new_version": new_version}))
        return self._open_change(
            conn, actor_id=actor_id, change_type="route_adjustment",
            origin_type="metric", origin_id=f"{metric_id}#v{new_version}",
            reason=reason or "指标口径发生不兼容变更",
            payload={"metric_id": metric_id, "new_version": new_version},
            effects=self._dedupe_effects(effects))

    def _dedupe_effects(self, effects):
        seen: set[tuple[str, str, str]] = set()
        unique = []
        for effect in effects:
            key = (effect[0], effect[1], effect[2])
            if key not in seen:
                seen.add(key)
                unique.append(effect)
        return unique

    # ------------------------------------------------------------------
    # 支付与关账
    # ------------------------------------------------------------------
    def mark_installment_paid(self, *, request_id: str, actor_id: str,
                              installment_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "installment_id": installment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.mark_installment_paid", payload=body)
            if replay is not None:
                return replay
            installment_id = self._id(installment_id, "installment_id")
            row = conn.execute(
                "SELECT * FROM mc_budget_ledger WHERE installment_id=? AND status IN ('released')",
                (installment_id,)).fetchone()
            if row is None:
                raise ConflictError("分期尚未释放，不能登记支付")

            def create():
                conn.execute(
                    "UPDATE mc_budget_ledger SET status='paid', paid_at=? WHERE ledger_id=?",
                    (self._now(), row["ledger_id"]))
                self._audit(conn, actor_id=actor_id, action="mc.budget.paid",
                            resource_type="budget_ledger", resource_id=row["ledger_id"],
                            detail={"installment_id": installment_id})
                return "budget_ledger", row["ledger_id"], {"ledger_id": row["ledger_id"],
                                                           "status": "paid"}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.mark_installment_paid", payload=body, create=create)

    def close_installment(self, *, request_id: str, actor_id: str,
                          installment_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "installment_id": installment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_or_none(conn, request_id=request_id,
                                                      action="mc.close_installment", payload=body)
            if replay is not None:
                return replay
            installment_id = self._id(installment_id, "installment_id")
            row = conn.execute(
                "SELECT * FROM mc_budget_ledger WHERE installment_id=? AND status='paid'",
                (installment_id,)).fetchone()
            if row is None:
                raise ConflictError("分期尚未支付，不能关账")

            def create():
                conn.execute(
                    "UPDATE mc_budget_ledger SET status='closed', closed_at=? WHERE ledger_id=?",
                    (self._now(), row["ledger_id"]))
                self._audit(conn, actor_id=actor_id, action="mc.budget.closed",
                            resource_type="budget_ledger", resource_id=row["ledger_id"],
                            detail={"installment_id": installment_id})
                return "budget_ledger", row["ledger_id"], {"ledger_id": row["ledger_id"],
                                                           "status": "closed"}

            return self._idempotent(conn, request_id=request_id,
                                    action="mc.close_installment", payload=body, create=create)

    # ------------------------------------------------------------------
    # 管理查询
    # ------------------------------------------------------------------
    def gate_status(self, gate_milestone_id: str) -> dict[str, Any]:
        """返回阶段门现状与资金尚未释放的确切原因。"""

        with self.database.transaction() as conn:
            gate = self._gate_or_404(conn, gate_milestone_id)
            passed = self._effective_passed(conn, gate)
            decision = self._current_open_round(conn, gate)
            if decision is None:
                # 回退到最近一轮已决定记录，用于展示结论与留痕
                decision = conn.execute(
                    "SELECT * FROM mc_gate_decisions WHERE gate_milestone_id=? "
                    "ORDER BY round DESC LIMIT 1", (gate,)).fetchone()
            acceptance = None
            if decision is not None and decision["acceptance_id"]:
                acceptance = conn.execute("SELECT * FROM mc_acceptances WHERE acceptance_id=?",
                                          (decision["acceptance_id"],)).fetchone()
            if passed is not None:
                reasons: list[dict[str, Any]] = []
            elif decision is not None and decision["state"] == "open":
                reasons = self._block_reasons(conn, gate, decision, acceptance)
            else:
                reasons = [{"code": "round_not_open", "gate": gate,
                            "message": "会签轮次尚未开启"}]
            signoffs = []
            if decision is not None:
                signoffs = [dict(s) for s in conn.execute(
                    "SELECT expert_id,opinion,status,signed_at FROM mc_gate_signoffs "
                    "WHERE decision_id=? ORDER BY signed_at", (decision["decision_id"],)).fetchall()]
            installments = []
            released_total = 0
            for row in conn.execute(
                    "SELECT i.installment_id,i.amount_cents,i.currency,i.status AS istatus,"
                    "l.ledger_id,l.status AS lstatus FROM mc_budget_installments i "
                    "LEFT JOIN mc_budget_ledger l ON l.installment_id=i.installment_id "
                    "AND l.status IN ('released','paid','closed') "
                    "WHERE i.gate_milestone_id=? ORDER BY i.sequence_no", (gate,)).fetchall():
                item = {"installment_id": row["installment_id"], "amount_cents": row["amount_cents"],
                        "currency": row["currency"], "status": row["istatus"]}
                if row["ledger_id"]:
                    item.update(ledger_id=row["ledger_id"], ledger_status=row["lstatus"])
                    if row["lstatus"] in ("released", "paid", "closed"):
                        released_total += row["amount_cents"]
                installments.append(item)
            return {
                "gate_milestone_id": gate,
                "current_version": self._milestone_version(conn, gate),
                "state": "passed" if passed else (decision["state"] if decision else "idle"),
                "round": decision["round"] if decision else None,
                "passed_decision_id": passed["decision_id"] if passed else None,
                "acceptance": None if acceptance is None else {
                    "acceptance_id": acceptance["acceptance_id"],
                    "result": acceptance["result"], "status": acceptance["status"],
                    "basis_hash": acceptance["basis_hash"]},
                "signoffs": signoffs,
                "installments": installments,
                "released_total_cents": released_total,
                "block_reasons": reasons,
            }

    def pending_funds(self, project_id: str) -> dict[str, Any]:
        """列出专项内尚未释放的分期及其被阻断的确切原因。"""

        with self.database.transaction() as conn:
            project_id = self._id(project_id, "project_id")
            if not conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("专项不存在")
            items = []
            for row in conn.execute(
                    "SELECT * FROM mc_budget_installments WHERE project_id=? AND status='planned' "
                    "ORDER BY gate_milestone_id, sequence_no", (project_id,)).fetchall():
                gate = row["gate_milestone_id"]
                decision = self._current_open_round(conn, gate)
                acceptance = None
                if decision is not None and decision["acceptance_id"]:
                    acceptance = conn.execute("SELECT * FROM mc_acceptances WHERE acceptance_id=?",
                                              (decision["acceptance_id"],)).fetchone()
                reasons = self._block_reasons(conn, gate, decision, acceptance) if decision else [
                    {"code": "round_not_open", "gate": gate, "message": "会签轮次尚未开启"}]
                items.append({"installment_id": row["installment_id"], "topic_id": row["topic_id"],
                              "gate_milestone_id": gate, "sequence_no": row["sequence_no"],
                              "amount_cents": row["amount_cents"], "currency": row["currency"],
                              "block_reasons": reasons})
            return {"project_id": project_id, "pending_cents": sum(i["amount_cents"] for i in items),
                    "items": items}

    def critical_path(self, project_id: str) -> dict[str, Any]:
        """计算当前关键路径（终止节点剔除，已通过节点工期视为 0）。"""

        with self.database.transaction() as conn:
            project_id = self._id(project_id, "project_id")
            if not conn.execute("SELECT 1 FROM mc_projects WHERE project_id=?", (project_id,)).fetchone():
                raise NotFoundError("专项不存在")
            graph, reverse = self._load_graph(conn, project_id)
            rows = conn.execute(
                "SELECT m.milestone_id, v.duration_days, m.status FROM mc_milestones m "
                "JOIN mc_milestone_versions v ON m.milestone_id=v.milestone_id "
                "AND m.current_version=v.version WHERE v.project_id=?", (project_id,)).fetchall()
            active = {r["milestone_id"] for r in rows if r["status"] != "terminated"}
            durations = {r["milestone_id"]: r["duration_days"] for r in rows
                         if r["milestone_id"] in active}
            for node in list(graph):
                graph[node] = {n for n in graph[node] if n in active}
            completed = {node for node in active if self._effective_passed(conn, node)}
            roots = sorted(node for node in active if not reverse.get(node, set()) & active)
            path = critical_path(graph, durations, completed, roots)
            return {"project_id": project_id, "critical_path": path,
                    "completed": sorted(completed), "roots": roots}

    def change_impact(self, change_id: str) -> dict[str, Any]:
        """查看一次变化影响了哪些门、预算与交付承诺。"""

        with self.database.transaction() as conn:
            change_id = self._id(change_id, "change_id")
            order = conn.execute("SELECT * FROM mc_change_orders WHERE change_id=?",
                                 (change_id,)).fetchone()
            if order is None:
                raise NotFoundError("变更单不存在")
            effects = []
            for row in conn.execute(
                    "SELECT * FROM mc_change_effects WHERE change_id=? ORDER BY sequence_no",
                    (change_id,)).fetchall():
                item = {"effect_id": row["effect_id"], "target_type": row["target_type"],
                        "target_id": row["target_id"], "effect_type": row["effect_type"],
                        "status": row["status"], "detail": json.loads(row["detail_json"])}
                if row["target_type"] == "commitment":
                    v = conn.execute(
                        "SELECT title,milestone_id,metric_id,metric_version FROM mc_commitment_versions "
                        "WHERE commitment_id=? ORDER BY version DESC LIMIT 1",
                        (row["target_id"],)).fetchone()
                    if v is not None:
                        item["commitment"] = {"title": v["title"], "milestone_id": v["milestone_id"],
                                              "metric_id": v["metric_id"],
                                              "metric_version": v["metric_version"]}
                effects.append(item)
            open_count = sum(1 for e in effects if e["status"] == "open")
            return {"change_id": change_id, "change_type": order["change_type"],
                    "origin_type": order["origin_type"], "origin_id": order["origin_id"],
                    "reason": order["reason"], "status": order["status"],
                    "payload": json.loads(order["payload_json"]),
                    "created_at": order["created_at"], "effects": effects,
                    "open_effects": open_count,
                    "affected_commitments": [e["target_id"] for e in effects
                                             if e["target_type"] == "commitment"]}

    def list_changes(self, *, origin_type: str | None = None,
                     origin_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as conn:
            sql = "SELECT * FROM mc_change_orders"
            params: list[Any] = []
            clauses = []
            if origin_type:
                clauses.append("origin_type=?")
                params.append(origin_type)
            if origin_id:
                clauses.append("origin_id=?")
                params.append(origin_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at, change_id"
            items = [{"change_id": r["change_id"], "change_type": r["change_type"],
                      "origin_type": r["origin_type"], "origin_id": r["origin_id"],
                      "reason": r["reason"], "status": r["status"],
                      "created_at": r["created_at"]}
                     for r in conn.execute(sql, params).fetchall()]
            return {"items": items}

    def list_versions(self, entity: str, entity_id: str) -> dict[str, Any]:
        """只读查看某实体固化过的全部版本（旧版结论可审计）。"""

        tables = {
            "project": ("mc_project_versions", "project_id"),
            "topic": ("mc_topic_versions", "topic_id"),
            "work_package": ("mc_workpackage_versions", "wp_id"),
            "metric": ("mc_metric_versions", "metric_id"),
            "evidence": ("mc_evidence_versions", "evidence_id"),
            "expert": ("mc_expert_versions", "expert_id"),
            "milestone": ("mc_milestone_versions", "milestone_id"),
            "commitment": ("mc_commitment_versions", "commitment_id"),
            "dependency": ("mc_dependency_versions", "dependency_id"),
        }
        if entity not in tables:
            raise ValidationError("未知实体类型")
        table, id_column = tables[entity]
        with self.database.transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {id_column}=? ORDER BY version",
                (entity_id,)).fetchall()
            if not rows:
                raise NotFoundError("实体不存在")
            return {"entity": entity, "entity_id": entity_id,
                    "versions": [dict(r) for r in rows]}
