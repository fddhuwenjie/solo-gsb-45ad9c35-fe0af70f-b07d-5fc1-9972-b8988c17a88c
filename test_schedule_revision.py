"""轮灌计划防覆盖修订（乐观并发）回归测试。

运行（无需启动服务，使用 Flask 测试客户端 + 临时 SQLite 库）：

    python3 -m unittest test_schedule_revision -v

覆盖：
- 页面加载取得修订号，计划写入原子校验基线并递增；
- 两个窗口基于同一版本：先提交成功、后提交 409 且不产生部分更新；
- 合并期间版本再次变化仍被拒绝（重试可成功）；
- 阀门流量 / 并发、分区约束、停水时段、总流量变更均使旧基线失效；
- 仅改名 / 仅改计划日期 / 仅改水源绑定不递增修订；
- 不带 base_revision 的旧客户端保持兼容（单窗口直接写入）；
- 校验与导出接口保持兼容（导出携带 plan_revision）。
"""

import os
import tempfile
import unittest

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "rev-test.db"))

import app as appmod


class RevisionTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmpdir, "rev-test.db")
        appmod.DB_PATH = cls.db_path
        appmod.app.testing = True

    def setUp(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        appmod.init_db()
        self.c = appmod.app.test_client()

    def state(self):
        return self.c.get("/api/state").get_json()

    def reset_sample(self):
        return self.c.post("/api/reset", json={"sample": True}).get_json()

    def put_schedule(self, items, base_revision):
        return self.c.put("/api/schedule",
                          json={"items": items, "base_revision": base_revision})


class RevisionBasicsTests(RevisionTestBase):

    def test_state_exposes_revision_from_one(self):
        s = self.state()
        self.assertIn("revision", s)
        self.assertEqual(s["revision"], 1)
        self.assertEqual(len(s["items"]), 5)  # 内置示例已编排

    def test_legacy_client_without_baseline_still_writes(self):
        # 不带 base_revision：兼容旧客户端，直接写入并递增
        r = self.c.put("/api/schedule", json={"items": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], 2)
        self.assertEqual(self.state()["items"], [])

    def test_matching_baseline_commits_and_increments(self):
        base = self.state()["revision"]
        r = self.put_schedule([], base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base + 1)

    def test_stale_baseline_rejected_without_partial_update(self):
        s = self.state()
        base = s["revision"]
        # 窗口 A 先提交成功
        ok = self.put_schedule([], base)
        self.assertEqual(ok.status_code, 200)
        new_rev = ok.get_json()["revision"]
        # 窗口 B 用同一旧基线提交：409，响应附最新完整状态
        stale_items = [{"zone_id": s["zones"][0]["id"], "start_slot": 0, "length": 1}]
        r = self.put_schedule(stale_items, base)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["conflict"], "revision")
        self.assertEqual(body["current_revision"], new_rev)
        self.assertEqual(body["state"]["revision"], new_rev)
        # 服务器保持 A 的提交结果：空计划，未写入 B 的时段，修订未再递增
        after = self.state()
        self.assertEqual(after["revision"], new_rev)
        self.assertEqual(after["items"], [])

    def test_rejected_payload_is_not_half_written(self):
        s = self.state()
        base = s["revision"]
        self.assertEqual(self.put_schedule([], base).status_code, 200)
        # 旧基线 + 非法分区混合：必须先校验内容再开事务；即使基线过期，
        # 409 与 400 都不能留下 DELETE 后的残缺计划
        bad = [{"zone_id": 999999, "start_slot": 0, "length": 1}]
        r_bad = self.put_schedule(bad, base)
        self.assertEqual(r_bad.status_code, 400)  # 内容非法先返回 400
        self.assertEqual(self.state()["items"], [])

    def test_retry_with_latest_revision_succeeds(self):
        base = self.state()["revision"]
        self.assertEqual(self.put_schedule([], base).status_code, 200)
        latest = self.state()["revision"]
        r = self.put_schedule([], latest)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], latest + 1)

    def test_generate_uses_baseline_and_is_atomic_on_conflict(self):
        s = self.state()
        base = s["revision"]
        # 窗口 A 用空计划占位先提交
        self.assertEqual(self.put_schedule([], base).status_code, 200)
        # 窗口 B 基于旧基线自动编排 -> 409，且计划 / 修订不变
        before = self.state()
        r = self.c.post("/api/generate", json={"base_revision": base})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["conflict"], "revision")
        after = self.state()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual([i["zone_id"] for i in after["items"]], [])

    def test_invalid_baseline_param(self):
        for bad in ("abc", 0, -3, 1.5, True):
            r = self.put_schedule([], bad)
            self.assertEqual(r.status_code, 400, f"base_revision={bad!r}")

    def test_concurrent_first_writer_wins_simulation(self):
        # 模拟两窗口加载同一修订后先后提交
        win_a = self.state()
        win_b = self.state()
        self.assertEqual(win_a["revision"], win_b["revision"])
        zid = win_a["zones"][0]["id"]
        a_items = [{"zone_id": zid, "start_slot": 0, "length": 2}]
        b_items = [{"zone_id": zid, "start_slot": 40, "length": 2}]
        ra = self.put_schedule(a_items, win_a["revision"])
        rb = self.put_schedule(b_items, win_b["revision"])
        self.assertEqual(ra.status_code, 200)
        self.assertEqual(rb.status_code, 409)
        self.assertEqual(self.state()["items"][0]["start_slot"], 0)  # 先提交者生效


class ConstraintInvalidationTests(RevisionTestBase):

    def test_total_flow_change_bumps_revision(self):
        base = self.state()["revision"]
        r = self.c.post("/api/settings", json={"total_flow": "80"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base + 1)
        # 旧基线立即失效
        self.assertEqual(self.put_schedule([], base).status_code, 409)

    def test_same_total_flow_does_not_bump(self):
        total = self.state()["settings"]["total_flow"]
        base = self.state()["revision"]
        r = self.c.post("/api/settings", json={"total_flow": str(total)})
        self.assertEqual(r.get_json()["revision"], base)

    def test_plan_date_change_does_not_bump(self):
        base = self.state()["revision"]
        r = self.c.post("/api/settings", json={"plan_date": "2030-01-01"})
        self.assertEqual(r.get_json()["revision"], base)

    def test_valve_flow_and_concur_bump_name_only_does_not(self):
        v = self.state()["valves"][0]
        base = self.state()["revision"]
        payload = {"name": v["name"], "flow_lmin": v["flow_lmin"], "concur": v["concur"]}
        r = self.c.put(f"/api/valves/{v['id']}", json=payload)
        self.assertEqual(r.get_json()["revision"], base)
        r = self.c.put(f"/api/valves/{v['id']}",
                       json={"name": v["name"], "flow_lmin": 99, "concur": v["concur"]})
        self.assertEqual(r.get_json()["revision"], base + 1)
        self.assertEqual(self.put_schedule([], base).status_code, 409)

        latest = self.state()["revision"]
        r = self.c.put(f"/api/valves/{v['id']}",
                       json={"name": v["name"], "flow_lmin": 99, "concur": 2})
        self.assertEqual(r.get_json()["revision"], latest + 1)

    def test_valve_source_binding_change_does_not_bump(self):
        # 仅水源绑定（水质页）变化不应使计划基线失效
        s = self.state()
        if not s["valves"]:
            self.skipTest("无阀门")
        vid = s["valves"][0]["id"]
        base = s["revision"]
        r = self.c.put(f"/api/valves/{vid}/source", json={"source_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base)

    def test_zone_constraint_change_bumps_name_only_does_not(self):
        z = self.state()["zones"][0]
        base = self.state()["revision"]
        payload = {"name": z["name"], "water_l": z["water_l"], "valve_id": z["valve_id"],
                   "earliest": z["earliest"], "latest": z["latest"]}
        r = self.c.put(f"/api/zones/{z['id']}", json=payload)
        self.assertEqual(r.get_json()["revision"], base)
        r = self.c.put(f"/api/zones/{z['id']}",
                       json={**payload, "water_l": z["water_l"] + 100})
        self.assertEqual(r.get_json()["revision"], base + 1)

        latest = self.state()["revision"]
        r = self.c.put(f"/api/zones/{z['id']}",
                       json={**payload, "water_l": z["water_l"] + 100,
                             "earliest": "05:00"})
        self.assertEqual(r.get_json()["revision"], latest + 1)

    def test_blackout_changes_bump(self):
        base = self.state()["revision"]
        r = self.c.post("/api/blackouts",
                        json={"name": "临时停水", "start": "12:00", "end": "12:30"})
        self.assertEqual(r.status_code, 201)
        bid = r.get_json()["id"]
        self.assertEqual(self.state()["revision"], base + 1)
        r = self.c.put(f"/api/blackouts/{bid}",
                       json={"name": "临时停水", "start": "13:00", "end": "13:30"})
        self.assertEqual(r.get_json()["revision"], base + 2)
        r = self.c.delete(f"/api/blackouts/{bid}")
        self.assertEqual(r.get_json()["revision"], base + 3)

    def test_zone_create_and_delete_bump(self):
        s = self.state()
        base = s["revision"]
        vid = s["valves"][0]["id"]
        r = self.c.post("/api/zones", json={
            "name": "新分区XYZ", "water_l": 300, "valve_id": vid,
            "earliest": "06:00", "latest": "09:00"})
        self.assertEqual(r.status_code, 201)
        zid = r.get_json()["id"]
        self.assertEqual(self.state()["revision"], base + 1)
        r = self.c.delete(f"/api/zones/{zid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base + 2)

    def test_valve_create_and_delete_bump(self):
        base = self.state()["revision"]
        r = self.c.post("/api/valves",
                        json={"name": "临时干管", "flow_lmin": 20, "concur": 1})
        self.assertEqual(r.status_code, 201)
        vid = r.get_json()["id"]
        self.assertEqual(self.state()["revision"], base + 1)
        r = self.c.delete(f"/api/valves/{vid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base + 2)

    def test_reset_bumps_revision(self):
        base = self.state()["revision"]
        r = self.c.post("/api/reset", json={"sample": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["revision"], base + 1)
        r = self.c.post("/api/reset", json={"sample": True})
        self.assertEqual(r.get_json()["revision"], base + 2)


class CompatibilityTests(RevisionTestBase):

    def test_validation_still_reports_conflicts(self):
        # 校验结果结构保持不变：把分区拖到停水时段应仍有 issues
        s = self.reset_sample()
        zid = next(z["id"] for z in s["zones"] if z["name"] == "草莓区")
        items = [{"zone_id": i["zone_id"], "start_slot": i["start_slot"],
                  "length": i["length"]} for i in s["items"] if i["zone_id"] != zid]
        items.append({"zone_id": zid, "start_slot": 30, "length": 4})  # 07:30 停水
        r = self.put_schedule(items, s["revision"])
        self.assertEqual(r.status_code, 200)
        ev = r.get_json()["evaluation"]
        self.assertTrue(any(i["zone_id"] == zid for i in ev["issues"]))

    def test_export_carries_revision_and_stays_compatible(self):
        s = self.state()
        body = self.c.get("/api/export").get_json()
        self.assertEqual(body["plan_revision"], s["revision"])
        for key in ("plan_date", "slot_minutes", "total_flow_limit_lmin",
                    "actual_peak_flow_lmin", "valid", "blackouts",
                    "planned", "unscheduled"):
            self.assertIn(key, body)

    def test_state_shape_keeps_legacy_keys(self):
        s = self.state()
        for key in ("settings", "valves", "zones", "blackouts", "items", "evaluation"):
            self.assertIn(key, s)


if __name__ == "__main__":
    unittest.main()
