#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


def normalize_models(value: Any) -> list[str]:
    """适用机型列表；["*"] 表示不限机型。"""
    if value is None: return ["*"]
    if not isinstance(value, list) or not value: raise ApiError(400, "invalid_models", "适用机型必须是非空列表，如 [\"M400\"] 或 [\"*\"]")
    models = [str(item).strip() for item in value]
    if any(not model for model in models): raise ApiError(400, "invalid_models", "适用机型不能包含空值")
    return sorted(set(models))


def peak_occupancy(intervals: list[tuple[datetime, datetime]]) -> int:
    """扫描线求同时占用峰值，用于容量校验（半开区间，结束即释放）。"""
    events: list[tuple[datetime, int]] = []
    for start, end in intervals:
        events.append((start, 1)); events.append((end, -1))
    peak = current = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        current += delta; peak = max(peak, current)
    return peak


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS diversion_points(
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, models_json TEXT NOT NULL DEFAULT '["*"]',
            capacity INTEGER NOT NULL, opens_at TEXT NOT NULL, closes_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS diversion_occupancy(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), point_id INTEGER NOT NULL REFERENCES diversion_points(id),
            slot_kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'held', acquired_at TEXT NOT NULL, released_at TEXT, release_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS diversion_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), action TEXT NOT NULL,
            from_point_id INTEGER, to_point_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        """)
        plan_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(flight_plans)")}
        if "primary_alternate_id" not in plan_cols: self.conn.execute("ALTER TABLE flight_plans ADD COLUMN primary_alternate_id INTEGER")
        if "backup_alternate_id" not in plan_cols: self.conn.execute("ALTER TABLE flight_plans ADD COLUMN backup_alternate_id INTEGER")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            return dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone())

    # ---- 备降协同 ----

    @staticmethod
    def _point_row(conn: sqlite3.Connection, point_id: Any) -> sqlite3.Row:
        try: pid = int(point_id)
        except (TypeError, ValueError): raise ApiError(400, "invalid_alternate", "备降点编号必须是整数")
        row = conn.execute("SELECT * FROM diversion_points WHERE id=?", (pid,)).fetchone()
        if not row: raise ApiError(404, "alternate_not_found", f"备降点 {point_id} 不存在")
        return row

    @staticmethod
    def _point_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "code": row["code"], "name": row["name"], "models": json.loads(row["models_json"]),
                "capacity": row["capacity"], "opens_at": row["opens_at"], "closes_at": row["closes_at"], "status": row["status"]}

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row); item["detail"] = json.loads(item.pop("detail_json")); return item

    @staticmethod
    def _occupancy_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        plan = conn.execute("SELECT callsign,operator_id,starts_at,ends_at FROM flight_plans WHERE id=?", (row["plan_id"],)).fetchone()
        point = conn.execute("SELECT code,name FROM diversion_points WHERE id=?", (row["point_id"],)).fetchone()
        return {"occupancy_id": row["id"], "plan_id": row["plan_id"], "callsign": plan["callsign"] if plan else None,
                "operator_id": plan["operator_id"] if plan else None, "point_id": row["point_id"],
                "point_code": point["code"] if point else None, "point_name": point["name"] if point else None,
                "slot_kind": row["slot_kind"], "status": row["status"], "starts_at": plan["starts_at"] if plan else None,
                "ends_at": plan["ends_at"] if plan else None, "acquired_at": row["acquired_at"],
                "released_at": row["released_at"], "release_reason": row["release_reason"]}

    def create_diversion_point(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "alternate_forbidden", "只有空域审核员或指挥官可以维护备降点")
        code, name = str(body.get("code", "")).strip().upper(), str(body.get("name", "")).strip()
        if not code or not name: raise ApiError(400, "invalid_alternate", "备降点代码和名称必填")
        models = normalize_models(body.get("models"))
        try: capacity = int(body.get("capacity"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_alternate", "同时容量必须是正整数")
        if capacity < 1: raise ApiError(400, "invalid_alternate", "同时容量必须是正整数")
        opens, closes = parse_time(body.get("opens_at")), parse_time(body.get("closes_at"))
        if closes <= opens: raise ApiError(400, "invalid_alternate", "开放时段无效：关闭时间必须晚于开放时间")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO diversion_points(code,name,models_json,capacity,opens_at,closes_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                   (code, name, json.dumps(models, ensure_ascii=False), capacity, iso(opens), iso(closes), actor, iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "alternate_duplicate", f"备降点代码 {code} 已存在") from exc
            Repository.audit(conn, None, actor, role, "diversion_point_created", {"point_id": cur.lastrowid, "code": code, "models": models, "capacity": capacity})
            return self._point_summary(conn.execute("SELECT * FROM diversion_points WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_diversion_point(self, point_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "alternate_forbidden", "只有空域审核员或指挥官可以维护备降点")
        with self.repo.tx() as conn:
            point = self._point_row(conn, point_id)
            name = str(body.get("name", point["name"])).strip()
            models = normalize_models(body["models"]) if "models" in body else json.loads(point["models_json"])
            capacity = int(body.get("capacity", point["capacity"]))
            opens = parse_time(body.get("opens_at", point["opens_at"])); closes = parse_time(body.get("closes_at", point["closes_at"]))
            status = str(body.get("status", point["status"])).strip()
            if not name or capacity < 1 or closes <= opens or status not in {"active", "closed"}:
                raise ApiError(400, "invalid_alternate", "名称、容量、开放时段或状态无效")
            conn.execute("UPDATE diversion_points SET name=?,models_json=?,capacity=?,opens_at=?,closes_at=?,status=? WHERE id=?",
                         (name, json.dumps(models, ensure_ascii=False), capacity, iso(opens), iso(closes), status, point["id"]))
            Repository.audit(conn, None, actor, role, "diversion_point_updated", {"point_id": point["id"], "code": point["code"], "models": models, "capacity": capacity, "status": status})
            return self._point_summary(conn.execute("SELECT * FROM diversion_points WHERE id=?", (point["id"],)).fetchone())

    def list_diversion_points(self, role: str) -> dict[str, Any]:
        conn = self.repo.conn
        points = [self._point_summary(row) for row in conn.execute("SELECT * FROM diversion_points ORDER BY id")]
        if role == "viewer":
            points = [{key: point[key] for key in ("id", "code", "name", "opens_at", "closes_at", "status")} for point in points]
        return {"diversion_points": points}

    def _validate_alternate_selection(self, conn: sqlite3.Connection, primary_id: Any, backup_id: Any) -> tuple[int | None, int | None]:
        primary = self._point_row(conn, primary_id)["id"] if primary_id not in (None, "") else None
        backup = self._point_row(conn, backup_id)["id"] if backup_id not in (None, "") else None
        if primary is not None and backup is not None and primary == backup: raise ApiError(400, "invalid_alternate", "主备降点和备用备降点不能相同")
        return primary, backup

    def _check_alternate_eligibility(self, conn: sqlite3.Connection, plan: sqlite3.Row, point: sqlite3.Row, exclude_plan_id: int | None = None) -> dict[str, Any]:
        """校验备降点能否接收该计划：机型匹配、开放时段覆盖、同时容量未满。"""
        start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        reasons: list[dict[str, Any]] = []
        models = json.loads(point["models_json"])
        if "*" not in models and plan["drone_model"] not in models:
            reasons.append({"code": "model_mismatch", "message": f"机型 {plan['drone_model']} 不在备降点 {point['code']} 适用机型 {models} 内"})
        if start < parse_time(point["opens_at"]) or end > parse_time(point["closes_at"]):
            reasons.append({"code": "outside_open_hours", "message": f"计划时段 {plan['starts_at']}~{plan['ends_at']} 超出备降点开放时段 {point['opens_at']}~{point['closes_at']}"})
        held = list(conn.execute("""SELECT o.id,o.plan_id,p.callsign,p.starts_at,p.ends_at FROM diversion_occupancy o
                                    JOIN flight_plans p ON p.id=o.plan_id
                                    WHERE o.point_id=? AND o.status='held' AND p.starts_at<? AND p.ends_at>?""",
                                 (point["id"], iso(end), iso(start))))
        overlapping = [{"occupancy_id": row["id"], "plan_id": row["plan_id"], "callsign": row["callsign"], "starts_at": row["starts_at"], "ends_at": row["ends_at"]}
                       for row in held if row["plan_id"] != exclude_plan_id]
        intervals = [(parse_time(item["starts_at"]), parse_time(item["ends_at"])) for item in overlapping] + [(start, end)]
        if peak_occupancy(intervals) > point["capacity"]:
            reasons.append({"code": "capacity_full", "message": f"备降点 {point['code']} 同时容量 {point['capacity']} 已满", "capacity": point["capacity"], "held": len(overlapping)})
        return {"point": self._point_summary(point), "eligible": not reasons, "reasons": reasons, "occupancy": {"capacity": point["capacity"], "held_overlapping": overlapping}}

    @staticmethod
    def _insert_diversion_event(conn: sqlite3.Connection, plan_id: int, action: str, actor: str, role: str,
                                from_point_id: int | None = None, to_point_id: int | None = None, reason: str = "", detail: dict[str, Any] | None = None) -> None:
        conn.execute("INSERT INTO diversion_events(plan_id,action,from_point_id,to_point_id,actor,role,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (plan_id, action, from_point_id, to_point_id, actor, role, reason, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def _active_occupancy(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM diversion_occupancy WHERE plan_id=? AND status='held' ORDER BY id DESC", (plan_id,)).fetchone()

    def _release_occupancy(self, conn: sqlite3.Connection, plan_id: int, actor: str, role: str, reason: str) -> sqlite3.Row | None:
        held = self._active_occupancy(conn, plan_id)
        if not held: return None
        conn.execute("UPDATE diversion_occupancy SET status='released',released_at=?,release_reason=? WHERE id=?", (iso(), reason, held["id"]))
        self._insert_diversion_event(conn, plan_id, "released", actor, role, from_point_id=held["point_id"], reason=reason, detail={"slot_kind": held["slot_kind"]})
        return held

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            primary_id, backup_id = self._validate_alternate_selection(conn, body.get("primary_alternate_id"), body.get("backup_alternate_id"))
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,primary_alternate_id,backup_alternate_id,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], primary_id, backup_id, actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        else:
            for field, key in (("primary_alternate_id", "primary_alternate"), ("backup_alternate_id", "backup_alternate")):
                point = conn.execute("SELECT * FROM diversion_points WHERE id=?", (result[field],)).fetchone() if result.get(field) else None
                result[key] = self._point_summary(point) if point else None
            held = self._active_occupancy(conn, plan_id)
            result["diversion_occupancy"] = self._occupancy_summary(conn, held) if held else None
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            if plan["primary_alternate_id"]:
                point = self._point_row(conn, plan["primary_alternate_id"])
                if point["status"] != "active": raise ApiError(409, "alternate_unavailable", f"主备降点 {point['code']} 已关闭", self._point_summary(point))
                eligibility = self._check_alternate_eligibility(conn, plan, point, exclude_plan_id=plan_id)
                if not eligibility["eligible"]: raise ApiError(409, "alternate_unavailable", "主备降点无法接收该计划", eligibility)
                cur_occ = conn.execute("INSERT INTO diversion_occupancy(plan_id,point_id,slot_kind,acquired_at) VALUES(?,?,?,?)", (plan_id, point["id"], "primary", iso()))
                self._insert_diversion_event(conn, plan_id, "occupied", actor, role, to_point_id=point["id"], detail={"slot_kind": "primary", "occupancy_id": cur_occ.lastrowid})
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            primary_id, backup_id = self._validate_alternate_selection(conn, body.get("primary_alternate_id", plan["primary_alternate_id"]), body.get("backup_alternate_id", plan["backup_alternate_id"]))
            released = self._release_occupancy(conn, plan_id, actor, role, "plan_changed")
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,primary_alternate_id=?,backup_alternate_id=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), primary_id, backup_id, revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"], "occupancy_released": bool(released)})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            released = self._release_occupancy(conn, plan_id, actor, role, f"plan_canceled:{reason}")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason, "occupancy_released": bool(released)})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                released = self._release_occupancy(conn, row["id"], actor, role, "plan_expired")
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {"occupancy_released": bool(released)})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def divert(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"commander", "airspace_reviewer"}: raise ApiError(403, "divert_forbidden", "只有指挥官或空域审核员可以执行紧急改降")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "改降原因必填")
        target_id = body.get("target_alternate_id")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            held = self._active_occupancy(conn, plan_id)
            if not held: raise ApiError(409, "no_alternate_occupancy", "该计划没有可切换的备降点占用")
            from_point = self._point_row(conn, held["point_id"])
            target = self._point_row(conn, target_id) if target_id not in (None, "") else (self._point_row(conn, plan["backup_alternate_id"]) if plan["backup_alternate_id"] else None)
            if not target: raise ApiError(409, "no_backup_alternate", "计划未登记可用的备用备降点")
            if target["id"] == from_point["id"]: raise ApiError(409, "already_on_point", f"计划当前已占用备降点 {from_point['code']}")
            if target["status"] != "active":
                eligibility_block = {"point": self._point_summary(target), "eligible": False, "reasons": [{"code": "point_closed", "message": f"备降点 {target['code']} 已关闭"}], "occupancy": {"capacity": target["capacity"], "held_overlapping": []}}
            else:
                eligibility_block = self._check_alternate_eligibility(conn, plan, target, exclude_plan_id=plan_id)
            if not eligibility_block["eligible"]:
                # 备用点接不下：保留原安排，只记录这次失败的改降尝试
                self._insert_diversion_event(conn, plan_id, "divert_blocked", actor, role, from_point["id"], target["id"], reason, eligibility_block)
                Repository.audit(conn, plan_id, actor, role, "divert_blocked", {"from_point": from_point["code"], "to_point": target["code"], "reasons": eligibility_block["reasons"]})
                Repository.notify(conn, plan_id, "divert_blocked", f"飞行计划 {plan['callsign']} 改降 {target['code']} 失败，保留原备降点 {from_point['code']}")
                return {"diverted": False, "plan": self.get_plan(plan_id, role, ""), "from_alternate": self._point_summary(from_point), "target": eligibility_block}
            self._release_occupancy(conn, plan_id, actor, role, f"diverted:{target['code']}")
            cur = conn.execute("INSERT INTO diversion_occupancy(plan_id,point_id,slot_kind,acquired_at) VALUES(?,?,?,?)", (plan_id, target["id"], "backup", iso()))
            self._insert_diversion_event(conn, plan_id, "diverted", actor, role, from_point["id"], target["id"], reason, {"occupancy_id": cur.lastrowid})
            Repository.audit(conn, plan_id, actor, role, "diverted", {"from_point": from_point["code"], "to_point": target["code"], "reason": reason})
            Repository.notify(conn, plan_id, "diverted", f"飞行计划 {plan['callsign']} 已紧急改降到 {target['code']}，原备降点 {from_point['code']} 名额已释放")
            return {"diverted": True, "plan": self.get_plan(plan_id, role, ""), "from_alternate": self._point_summary(from_point), "to_alternate": self._point_summary(target), "occupancy_id": cur.lastrowid}

    def diversion_board(self, role: str, operator: str) -> dict[str, Any]:
        """协调台：余量、占用计划和改降记录。运营方只看本运营方数据；viewer 只看公开余量。"""
        conn = self.repo.conn
        all_held = list(conn.execute("""SELECT o.*, p.callsign AS p_callsign, p.operator_id AS p_operator_id, p.starts_at AS p_starts_at, p.ends_at AS p_ends_at
                                        FROM diversion_occupancy o JOIN flight_plans p ON p.id=o.plan_id
                                        WHERE o.status='held' ORDER BY o.id"""))
        if role == "operator":
            held_rows = [row for row in all_held if row["p_operator_id"] == operator]
        elif role == "viewer": held_rows = []
        else: held_rows = all_held
        event_rows = list(conn.execute("SELECT * FROM diversion_events ORDER BY id DESC LIMIT 100"))
        if role == "operator":
            event_rows = [row for row in event_rows if conn.execute("SELECT operator_id FROM flight_plans WHERE id=?", (row["plan_id"],)).fetchone()["operator_id"] == operator]
        if role == "viewer": event_rows = []
        now = utcnow()
        points: list[dict[str, Any]] = []
        for point in conn.execute("SELECT * FROM diversion_points ORDER BY id"):
            point_held = [row for row in all_held if row["point_id"] == point["id"]]
            current = [row for row in point_held if parse_time(row["p_starts_at"]) <= now < parse_time(row["p_ends_at"])]
            summary = self._point_summary(point)
            if role == "viewer": summary = {key: summary[key] for key in ("id", "code", "name", "opens_at", "closes_at", "status")}
            summary["held_total"] = len(point_held)
            summary["occupied_now"] = len(current)
            summary["remaining_now"] = max(0, point["capacity"] - len(current))
            points.append(summary)
        occupancy = [self._occupancy_summary(conn, row) for row in held_rows] if role != "viewer" else []
        events = [self._event_row(row) for row in event_rows] if role != "viewer" else []
        return {"diversion_points": points, "occupancy": occupancy, "diversion_events": events, "server_time": iso()}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "restrictions": restrictions, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        if path == "/api/diversion-points": return 200, self.service.list_diversion_points(role)
        if path == "/api/diversion-board": return 200, self.service.diversion_board(role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/diversion-points": return 201, self.service.create_diversion_point(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "diversion-points"] and parts[2].isdigit() and parts[3] == "update":
            return 200, self.service.update_diversion_point(int(parts[2]), actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
                "divert": lambda: self.service.divert(pid, actor, role, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
