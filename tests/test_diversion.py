import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from app import ApiError, DroneAirspaceService, iso, utcnow

REMOTE_ROUTE = [[10.1, 20.1], [10.3, 20.3]]


class DiversionCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2); self.end = self.start + timedelta(hours=1)

    def tearDown(self): self.tmp.cleanup()

    def point(self, code="ALT1", models=None, capacity=1, opens_shift=-1, closes_shift=3, actor="reviewer", role="airspace_reviewer"):
        return self.svc.create_diversion_point(actor, role, {"code": code, "name": f"备降点-{code}", "models": models or ["*"], "capacity": capacity,
                                                             "opens_at": iso(self.start + timedelta(hours=opens_shift)),
                                                             "closes_at": iso(self.start + timedelta(hours=closes_shift))})

    def plan(self, callsign="D200", route=None, model="M400", primary=None, backup=None, operator="OP1"):
        body = {"callsign": callsign, "drone_model": model, "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]],
                "starts_at": iso(self.start), "ends_at": iso(self.end), "max_altitude": 100, "population_risk": 1,
                "emergency_plan": "就近备降", "region": "BJ"}
        if primary: body["primary_alternate_id"] = primary
        if backup: body["backup_alternate_id"] = backup
        return self.svc.create_plan("op-user", "operator", operator, body)

    def approve(self, pid, offline, actor="reviewer", role="airspace_reviewer", revision=1, **extra):
        body = {"expected_revision": revision, "offline_id": offline, "reason": "复核"}; body.update(extra)
        return self.svc.approve(pid, actor, role, body)

    def submit_and_approve(self, callsign, offline, route=None, primary=None, backup=None):
        plan = self.plan(callsign, route=route, primary=primary, backup=backup)
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.approve(plan["id"], offline); return plan

    def test_approve_occupies_primary_and_plan_shows_it(self):
        primary = self.point("P1"); backup = self.point("B1")
        plan = self.submit_and_approve("D200", "off-1", primary=primary["id"], backup=backup["id"])
        view = self.svc.get_plan(plan["id"], "airspace_reviewer", "")
        self.assertEqual(view["primary_alternate"]["code"], "P1")
        self.assertEqual(view["diversion_occupancy"]["point_code"], "P1")
        board = self.svc.diversion_board("commander", "")
        p1 = next(p for p in board["diversion_points"] if p["code"] == "P1")
        self.assertEqual(p1["held_total"], 1); self.assertEqual(p1["remaining_now"], 1)  # 计划尚未开始
        self.assertEqual(len(board["occupancy"]), 1)
        self.assertEqual(board["occupancy"][0]["callsign"], "D200")
        self.assertEqual(board["diversion_events"][0]["action"], "occupied")

    def test_model_mismatch_rejected_with_occupancy(self):
        primary = self.point("P2", models=["X900"], capacity=2)
        plan = self.plan("D201", route=REMOTE_ROUTE, primary=primary["id"])
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan["id"], "off-2")
        self.assertEqual(ctx.exception.code, "alternate_unavailable")
        self.assertEqual(ctx.exception.details["reasons"][0]["code"], "model_mismatch")
        self.assertEqual(ctx.exception.details["occupancy"]["capacity"], 2)
        self.assertEqual(self.svc.get_plan(plan["id"], "commander", "")["status"], "submitted")

    def test_outside_open_hours_rejected(self):
        primary = self.point("P3", opens_shift=-1, closes_shift=0)  # 在计划结束前关闭
        plan = self.plan("D202", route=REMOTE_ROUTE, primary=primary["id"])
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(plan["id"], "off-3")
        self.assertEqual(ctx.exception.details["reasons"][0]["code"], "outside_open_hours")

    def test_capacity_full_rejected_with_held_plans(self):
        primary = self.point("P4", capacity=1)
        self.submit_and_approve("D203", "off-4a", route=REMOTE_ROUTE, primary=primary["id"])
        second = self.plan("D204", route=[[11.1, 21.1], [11.3, 21.3]], primary=primary["id"])
        self.svc.submit(second["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(second["id"], "off-4b")
        self.assertEqual(ctx.exception.code, "alternate_unavailable")
        self.assertEqual(ctx.exception.details["reasons"][0]["code"], "capacity_full")
        held = ctx.exception.details["occupancy"]["held_overlapping"]
        self.assertEqual(held[0]["callsign"], "D203")

    def test_emergency_divert_switches_and_releases_primary(self):
        primary = self.point("P5"); backup = self.point("B5")
        plan = self.submit_and_approve("D205", "off-5", primary=primary["id"], backup=backup["id"])
        result = self.svc.divert(plan["id"], "cmd", "commander", {"reason": "夜间禁飞临时扩大"})
        self.assertTrue(result["diverted"])
        self.assertEqual(result["from_alternate"]["code"], "P5"); self.assertEqual(result["to_alternate"]["code"], "B5")
        board = self.svc.diversion_board("commander", "")
        by_code = {p["code"]: p for p in board["diversion_points"]}
        self.assertEqual(by_code["P5"]["held_total"], 0); self.assertEqual(by_code["B5"]["held_total"], 1)
        self.assertEqual(self.svc.get_plan(plan["id"], "commander", "")["diversion_occupancy"]["point_code"], "B5")
        actions = [event["action"] for event in board["diversion_events"]]
        self.assertIn("diverted", actions); self.assertIn("released", actions)
        notes = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertEqual(notes[0]["kind"], "diverted")

    def test_divert_blocked_keeps_original_arrangement(self):
        primary = self.point("P6"); backup = self.point("B6")
        plan = self.submit_and_approve("D206", "off-6", primary=primary["id"], backup=backup["id"])
        # 另一计划占满备用点
        other = self.submit_and_approve("D207", "off-7", route=REMOTE_ROUTE, primary=backup["id"])
        result = self.svc.divert(plan["id"], "cmd", "commander", {"reason": "天气突变"})
        self.assertFalse(result["diverted"])
        self.assertEqual(result["target"]["reasons"][0]["code"], "capacity_full")
        self.assertEqual(self.svc.get_plan(plan["id"], "commander", "")["diversion_occupancy"]["point_code"], "P6")
        board = self.svc.diversion_board("commander", "")
        self.assertEqual(board["diversion_events"][0]["action"], "divert_blocked")
        # 备用点释放后改降成功
        self.svc.cancel(other["id"], "cmd", "commander", "", {"reason": "撤销"})
        again = self.svc.divert(plan["id"], "cmd", "commander", {"reason": "天气突变"})
        self.assertTrue(again["diverted"]); self.assertEqual(again["to_alternate"]["code"], "B6")

    def test_divert_without_backup_or_occupancy(self):
        primary = self.point("P7")
        plan = self.submit_and_approve("D208", "off-8", primary=primary["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.divert(plan["id"], "cmd", "commander", {"reason": "无备用点"})
        self.assertEqual(ctx.exception.code, "no_backup_alternate")
        no_slot = self.plan("D209", route=REMOTE_ROUTE)
        self.svc.submit(no_slot["id"], "op-user", "operator", "OP1", {})
        self.approve(no_slot["id"], "off-9")
        with self.assertRaises(ApiError) as ctx:
            self.svc.divert(no_slot["id"], "cmd", "commander", {"reason": "未登记备降"})
        self.assertEqual(ctx.exception.code, "no_alternate_occupancy")

    def test_change_cancel_and_expire_release_occupancy(self):
        primary = self.point("P8"); backup = self.point("B8")
        changed = self.submit_and_approve("D210", "off-10", primary=primary["id"], backup=backup["id"])
        view = self.svc.change(changed["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertIsNone(view["diversion_occupancy"])
        self.svc.submit(changed["id"], "op-user", "operator", "OP1", {})
        self.approve(changed["id"], "off-11", revision=2)
        self.svc.cancel(changed["id"], "cmd", "commander", "", {"reason": "任务取消"})
        self.assertEqual(self.svc.diversion_board("commander", "")["occupancy"], [])

        expired = self.submit_and_approve("D211", "off-12", route=REMOTE_ROUTE, primary=primary["id"])
        future = utcnow() + timedelta(hours=5)
        with mock.patch.object(app, "utcnow", return_value=future):
            result = self.svc.expire_plans("reviewer", "airspace_reviewer")
        self.assertEqual(result["expired"], 1)
        board = self.svc.diversion_board("commander", "")
        self.assertEqual(board["occupancy"], [])
        self.assertEqual(self.svc.get_plan(expired["id"], "commander", "")["status"], "expired")

    def test_board_scoping_and_roles(self):
        primary = self.point("P9"); backup = self.point("B9")
        self.submit_and_approve("D212", "off-13", primary=primary["id"], backup=backup["id"])
        other = self.plan("D213", route=REMOTE_ROUTE, primary=backup["id"], operator="OP2")
        self.svc.submit(other["id"], "op2", "operator", "OP2", {})
        self.approve(other["id"], "off-14")
        op1_board = self.svc.diversion_board("operator", "OP1")
        self.assertEqual({row["callsign"] for row in op1_board["occupancy"]}, {"D212"})
        self.assertTrue(all(event["plan_id"] != other["id"] for event in op1_board["diversion_events"]))
        # 余量对所有角色公开，但 viewer 看不到占用明细和改降记录
        viewer_board = self.svc.diversion_board("viewer", "")
        self.assertTrue(all("remaining_now" in point for point in viewer_board["diversion_points"]))
        self.assertEqual(viewer_board["occupancy"], []); self.assertEqual(viewer_board["diversion_events"], [])
        # 权限
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_diversion_point("op-user", "operator", {"code": "X", "name": "x", "capacity": 1, "opens_at": iso(self.start), "closes_at": iso(self.end)})
        self.assertEqual(ctx.exception.code, "alternate_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.divert(1, "op-user", "operator", {"reason": "无权限"})
        self.assertEqual(ctx.exception.code, "divert_forbidden")

    def test_point_update_and_invalid_selection(self):
        point = self.point("P10", capacity=1)
        updated = self.svc.update_diversion_point(point["id"], "reviewer", "airspace_reviewer", {"capacity": 3, "models": ["M400", "X900"]})
        self.assertEqual(updated["capacity"], 3); self.assertEqual(updated["models"], ["M400", "X900"])
        with self.assertRaises(ApiError) as ctx:
            self.plan("D214", primary=point["id"], backup=point["id"])
        self.assertEqual(ctx.exception.code, "invalid_alternate")
        with self.assertRaises(ApiError) as ctx:
            self.plan("D215", primary=999)
        self.assertEqual(ctx.exception.code, "alternate_not_found")


if __name__ == "__main__": unittest.main()
