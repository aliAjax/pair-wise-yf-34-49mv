import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class DroneFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def test_full_approval_change_and_offline_reconciliation(self):
        plan = self.plan(); submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])
        approved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "路线和应急方案满足要求"})
        self.assertEqual(approved["plan"]["status"], "approved")
        duplicate = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "补传"})
        self.assertTrue(duplicate["idempotent"])
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": submitted["revision"], "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["revision"], 2)
        notifications = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertEqual(notifications[0]["kind"], "approval_invalidated")

    def test_restriction_emergency_override_and_conflicts(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan("D101"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-2", "reason": "常规审核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        override = self.svc.approve(plan["id"], "commander", "commander", {"expected_revision": 1, "offline_id": "offline-3", "reason": "紧急任务", "override_reason": "应急救援授权"})
        self.assertEqual(override["plan"]["status"], "approved")
        conflicting = self.plan("D102", route=[[116.11, 39.81], [116.15, 39.84]])
        self.svc.submit(conflicting["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-4", "reason": "复核"})
        self.assertIn(ctx.exception.code, {"hard_constraint_violation", "airspace_conflict"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 99, "offline_id": "offline-5", "reason": "过期审核"})
        self.assertEqual(ctx.exception.code, "revision_conflict")


class AlternateCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def alternate(self, name="ALT-A", models=("M400",), capacity=1, open_start=None, open_end=None):
        return self.svc.create_alternate("reviewer", "airspace_reviewer", {"name": name, "models": list(models), "capacity": capacity,
            "open_start": open_start or (self.start - timedelta(hours=1)).strftime("%H:%M"),
            "open_end": open_end or (self.start + timedelta(hours=2)).strftime("%H:%M")})

    def plan(self, callsign="D200", primary=None, backup=None, route=None, model="M400"):
        body = {"callsign": callsign, "drone_model": model, "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]],
                "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100,
                "population_risk": 1, "emergency_plan": "备降或返航", "region": "BJ"}
        if primary is not None: body["primary_alternate_id"] = primary
        if backup is not None: body["backup_alternate_id"] = backup
        return self.svc.create_plan("op-user", "operator", "OP1", body)

    def approve(self, plan_id, offline_id, revision=1):
        return self.svc.approve(plan_id, "reviewer", "airspace_reviewer", {"expected_revision": revision, "offline_id": offline_id, "reason": "满足要求"})

    def site(self, board, alternate_id):
        return next(s for s in board["alternates"] if s["id"] == alternate_id)

    def test_approval_books_primary_and_board_shows_occupancy(self):
        alt = self.alternate(capacity=2)
        plan = self.plan(primary=alt["id"]); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.approve(plan["id"], "alt-1")
        detail = self.svc.get_plan(plan["id"], "commander")
        self.assertEqual(detail["alternate_booking"]["alternate_id"], alt["id"]); self.assertEqual(detail["alternate_booking"]["slot"], "primary")
        site = self.site(self.svc.alternates_board("commander"), alt["id"])
        self.assertEqual((site["occupied"], site["remaining"]), (1, 1)); self.assertEqual(site["bookings"][0]["callsign"], "D200")

    def test_approval_rejected_on_mismatch_closed_or_full_with_occupancy(self):
        mismatch = self.alternate("ALT-M", models=("M200",))
        plan = self.plan(primary=mismatch["id"]); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx: self.approve(plan["id"], "alt-2")
        self.assertEqual(ctx.exception.code, "alternate_model_mismatch"); self.assertIn("remaining", ctx.exception.details)
        closed = self.alternate("ALT-C", open_start=(self.start + timedelta(hours=2)).strftime("%H:%M"), open_end=(self.start + timedelta(hours=3)).strftime("%H:%M"))
        plan2 = self.plan("D201", primary=closed["id"], route=[[117.1, 40.8], [117.3, 40.9]]); self.svc.submit(plan2["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx: self.approve(plan2["id"], "alt-3")
        self.assertEqual(ctx.exception.code, "alternate_closed")
        full = self.alternate("ALT-F", capacity=1)
        first = self.plan("D202", primary=full["id"], route=[[118.1, 41.8], [118.3, 41.9]]); self.svc.submit(first["id"], "op-user", "operator", "OP1", {})
        self.approve(first["id"], "alt-4")
        second = self.plan("D203", primary=full["id"], route=[[119.1, 42.8], [119.3, 42.9]]); self.svc.submit(second["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx: self.approve(second["id"], "alt-5")
        self.assertEqual(ctx.exception.code, "alternate_full")
        self.assertEqual((ctx.exception.details["occupied"], ctx.exception.details["remaining"]), (1, 0))
        self.assertEqual(ctx.exception.details["occupants"][0]["callsign"], "D202")

    def test_emergency_diversion_switches_and_releases_primary(self):
        primary, backup = self.alternate("ALT-P"), self.alternate("ALT-B")
        plan = self.plan(primary=primary["id"], backup=backup["id"]); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.approve(plan["id"], "alt-6")
        result = self.svc.divert(plan["id"], "cmd", "commander", "", {"reason": "夜间天气突变"})
        self.assertEqual(result["result"], "switched")
        board = self.svc.alternates_board("commander")
        self.assertEqual(self.site(board, primary["id"])["occupied"], 0)
        self.assertEqual(self.site(board, backup["id"])["occupied"], 1)
        self.assertEqual(board["diversions"][0]["result"], "switched")
        self.assertEqual(board["diversions"][0]["from_alternate_name"], "ALT-P")
        with self.assertRaises(ApiError) as ctx: self.svc.divert(plan["id"], "cmd", "commander", "", {"reason": "再次改降"})
        self.assertEqual(ctx.exception.code, "already_diverted")

    def test_diversion_kept_when_backup_cannot_accept(self):
        primary, backup = self.alternate("ALT-P2"), self.alternate("ALT-B2", capacity=1)
        occupant = self.plan("D204", primary=backup["id"], route=[[117.1, 40.8], [117.3, 40.9]]); self.svc.submit(occupant["id"], "op-user", "operator", "OP1", {})
        self.approve(occupant["id"], "alt-7")
        plan = self.plan("D205", primary=primary["id"], backup=backup["id"]); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.approve(plan["id"], "alt-8")
        result = self.svc.divert(plan["id"], "cmd", "commander", "", {"reason": "主点无法使用"})
        self.assertEqual(result["result"], "kept"); self.assertEqual(result["cause"], "alternate_full")
        board = self.svc.alternates_board("commander")
        self.assertEqual(self.site(board, primary["id"])["occupied"], 1)
        self.assertEqual(self.site(board, backup["id"])["bookings"][0]["callsign"], "D204")
        self.assertEqual(board["diversions"][0]["result"], "kept")

    def test_change_cancel_and_expire_release_occupancy(self):
        alt = self.alternate("ALT-R")
        plan = self.plan(primary=alt["id"]); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.approve(plan["id"], "alt-9")
        self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.5, 39.5], [116.6, 39.6]]})
        self.assertEqual(self.site(self.svc.alternates_board("commander"), alt["id"])["occupied"], 0)
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {}); self.approve(plan["id"], "alt-10", revision=2)
        self.assertEqual(self.site(self.svc.alternates_board("commander"), alt["id"])["occupied"], 1)
        self.svc.cancel(plan["id"], "op-user", "operator", "OP1", {"reason": "任务取消"})
        self.assertEqual(self.site(self.svc.alternates_board("commander"), alt["id"])["occupied"], 0)
        expiring = self.plan("D206", primary=alt["id"], route=[[117.1, 40.8], [117.3, 40.9]]); self.svc.submit(expiring["id"], "op-user", "operator", "OP1", {})
        self.approve(expiring["id"], "alt-11")
        self.svc.repo.conn.execute("UPDATE flight_plans SET ends_at=? WHERE id=?", (iso(utcnow() - timedelta(minutes=1)), expiring["id"]))
        self.svc.expire_plans("reviewer", "airspace_reviewer")
        self.assertEqual(self.site(self.svc.alternates_board("commander"), alt["id"])["occupied"], 0)

    def test_alternate_maintenance_and_registration_validation(self):
        with self.assertRaises(ApiError) as ctx: self.svc.create_alternate("op-user", "operator", {})
        self.assertEqual(ctx.exception.code, "alternate_forbidden")
        alt = self.alternate()
        updated = self.svc.update_alternate(alt["id"], "reviewer", "airspace_reviewer", {"capacity": 3, "models": ["M400", "M200"]})
        self.assertEqual((updated["capacity"], updated["models"]), (3, ["M400", "M200"]))
        with self.assertRaises(ApiError) as ctx: self.plan("D207", primary=alt["id"], backup=alt["id"])
        self.assertEqual(ctx.exception.code, "invalid_alternate")
        with self.assertRaises(ApiError) as ctx: self.plan("D208", primary=9999)
        self.assertEqual(ctx.exception.code, "alternate_not_found")


if __name__ == "__main__": unittest.main()
