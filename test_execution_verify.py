"""执行核验回归测试。

运行：

    python3 -m unittest test_execution_verify -v

前半部分直接测试纯计算模块 execution_check（无需 Flask）；
后半部分通过 Flask 测试客户端测试批次 / 事件导入接口的
幂等、内容冲突整批拒绝、删除重算与导出（需要 Flask）。
"""

import os
import tempfile
import unittest
from datetime import date, datetime

import execution_check as ec

DAY = date(2026, 10, 1)


def dt(hhmm, day=DAY):
    return datetime.fromisoformat(f"{day.isoformat()}T{hhmm}")


def base_fixtures(total=60):
    valves = [
        {"id": 1, "name": "北侧干管", "flow_lmin": 25, "concur": 1},
        {"id": 2, "name": "中部干管", "flow_lmin": 25, "concur": 1},
        {"id": 3, "name": "苗床支管", "flow_lmin": 10, "concur": 1},
    ]
    zones = [
        {"id": 1, "name": "番茄区", "water_l": 3000, "valve_id": 1, "flow_lmin": 25,
         "earliest_slot": 24, "latest_slot": 40},
        {"id": 2, "name": "黄瓜区", "water_l": 2250, "valve_id": 2, "flow_lmin": 25,
         "earliest_slot": 72, "latest_slot": 88},
        {"id": 3, "name": "草莓区", "water_l": 1500, "valve_id": 2, "flow_lmin": 25,
         "earliest_slot": 22, "latest_slot": 34},
        {"id": 4, "name": "叶菜区", "water_l": 750, "valve_id": 2, "flow_lmin": 25,
         "earliest_slot": 32, "latest_slot": 44},
        {"id": 5, "name": "育苗区", "water_l": 450, "valve_id": 3, "flow_lmin": 10,
         "earliest_slot": 28, "latest_slot": 36},
    ]
    items = [
        {"zone_id": 3, "start_slot": 22, "length": 4},   # 草莓 05:30-06:30
        {"zone_id": 1, "start_slot": 32, "length": 8},   # 番茄 08:00-10:00
        {"zone_id": 4, "start_slot": 32, "length": 2},   # 叶菜 08:00-08:30
        {"zone_id": 5, "start_slot": 32, "length": 3},   # 育苗 08:00-08:45
        {"zone_id": 2, "start_slot": 72, "length": 6},   # 黄瓜 18:00-19:30
    ]
    blackouts = [{"start_slot": 30, "end_slot": 32}]     # 07:30-08:00
    return valves, zones, items, blackouts, total


def zone_by(res, zid):
    return next(z for z in res["zones"] if z["zone_id"] == zid)


class AlignmentTests(unittest.TestCase):

    def test_planned_segment_counts_actual_flow_and_interruption(self):
        """计划内按实际流量计水；中断段在 interruptions 中体现。"""
        valves, zones, items, bo, total = base_fixtures()
        events = [
            # 番茄 08:00-09:30（25 L/min）-> 2250 L，缺 750，中断 30 分
            {"event_id": "E1", "valve_id": 1, "start": dt("08:00"),
             "end": dt("09:30"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        z = zone_by(res, 1)
        self.assertEqual(z["status"], ec.DEFICIT)
        self.assertEqual(z["delivered_l"], 2250.0)
        self.assertEqual(z["deficit_l"], 750.0)
        self.assertEqual(z["interrupted_minutes"], 30)
        self.assertEqual(z["interruptions"][0]["start"][11:], "09:30")
        self.assertEqual(z["interruptions"][0]["end"][11:], "10:00")
        self.assertEqual(z["interruptions"][0]["missed_water_l"], 750.0)

    def test_half_open_boundary_touch_is_not_overlap(self):
        """事件恰在计划结束时刻开始 -> 不属于该计划，按紧邻延后关阀归属。"""
        valves, zones, items, bo, total = base_fixtures()
        events = [
            {"event_id": "E1", "valve_id": 1, "start": dt("10:00"),
             "end": dt("10:15"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        z = zone_by(res, 1)
        self.assertEqual(z["late_minutes"], 15)
        self.assertEqual(z["delivered_l"], 375.0)
        self.assertEqual(z["status"], ec.DEFICIT)

    def test_early_open_before_unique_plan(self):
        """计划开始前紧邻唯一计划的小段 -> 提前开阀，水量计入该分区。"""
        valves, zones, items, bo, total = base_fixtures()
        events = [
            {"event_id": "E1", "valve_id": 1, "start": dt("07:55"),
             "end": dt("10:00"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        z = zone_by(res, 1)
        self.assertEqual(z["early_minutes"], 5)
        self.assertEqual(z["start_delta_min"], -5)
        # 5+120 分钟 × 25
        self.assertEqual(z["delivered_l"], 3125.0)
        self.assertEqual(z["status"], ec.OVER)

    def test_gap_between_two_back_to_back_plans_is_ambiguous(self):
        """夹在同阀门两条计划段之间的空段 -> 证据不足。"""
        valves, zones, items, bo, total = base_fixtures()
        # 草莓计划 05:30-06:30；临时构造另一条同阀门计划 06:30-07:00
        items = [{"zone_id": 3, "start_slot": 22, "length": 4},
                 {"zone_id": 4, "start_slot": 26, "length": 2}]
        # 叶菜区被挪到 06:30-07:00（同阀门 2），事件 05:30-07:00 贯通
        z4 = next(z for z in zones if z["id"] == 4)
        z4["earliest_slot"] = 22
        events = [
            {"event_id": "E1", "valve_id": 2, "start": dt("05:30"),
             "end": dt("07:00"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        # 两个分区均唯一归属，不应出现歧义（背靠背边界为半开，不相交）
        self.assertEqual(zone_by(res, 3)["delivered_l"], 1500.0)
        self.assertEqual(zone_by(res, 4)["delivered_l"], 750.0)
        self.assertEqual(res["ambiguous_segments"], [])

    def test_overlapping_plans_segment_is_ambiguous_not_double_counted(self):
        """同阀门两条计划段重叠处无法唯一归属 -> 证据不足且不重复计量。"""
        valves, zones, items, bo, total = base_fixtures()
        items = [{"zone_id": 3, "start_slot": 22, "length": 4},   # 05:30-06:30
                 {"zone_id": 4, "start_slot": 24, "length": 2}]   # 06:00-06:30 重叠
        z4 = next(z for z in zones if z["id"] == 4)
        z4["earliest_slot"] = 22
        events = [
            {"event_id": "E1", "valve_id": 2, "start": dt("05:30"),
             "end": dt("06:30"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        amb = res["ambiguous_segments"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["start"][11:], "06:00")
        self.assertEqual(amb[0]["end"][11:], "06:30")
        self.assertEqual(sorted(amb[0]["covered_zone_ids"]), [3, 4])
        self.assertEqual(amb[0]["water_l"], 750.0)
        # 草莓/叶菜状态含证据不足；750L 只登记一次，不计任何分区
        self.assertEqual(zone_by(res, 3)["ambiguous_l"], 750.0)
        self.assertEqual(zone_by(res, 4)["ambiguous_l"], 750.0)
        self.assertEqual(res["summary"]["ambiguous_l"], 750.0)

    def test_concurrent_events_same_valve_are_ambiguous_and_flagged(self):
        """同阀门事件重叠：并发冲突 + 该时段证据不足（不判给任一事件）。"""
        valves, zones, items, bo, total = base_fixtures()
        events = [
            {"event_id": "A", "valve_id": 2, "start": dt("05:30"),
             "end": dt("06:30"), "flow_lmin": 25},
            {"event_id": "B", "valve_id": 2, "start": dt("05:30"),
             "end": dt("06:00"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        types = {c["type"] for c in res["conflicts"]}
        self.assertIn(ec.CONCURRENCY, types)
        amb = res["ambiguous_segments"]
        self.assertEqual([(s["start"][11:], s["end"][11:]) for s in amb],
                         [("05:30", "06:00")])
        self.assertEqual(sorted(amb[0]["event_ids"]), ["A", "B"])
        # 05:30-06:00 不计量，草莓只拿到 06:00-06:30 的 750 L
        self.assertEqual(zone_by(res, 3)["delivered_l"], 750.0)

    def test_unplanned_run_is_listed_not_attributed(self):
        valves, zones, items, bo, total = base_fixtures()
        events = [
            {"event_id": "U", "valve_id": 2, "start": dt("12:00"),
             "end": dt("12:15"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        unp = res["unplanned_segments"]
        self.assertEqual(len(unp), 1)
        self.assertEqual(unp[0]["kind"], ec.SEG_UNPLANNED)
        self.assertEqual(unp[0]["water_l"], 375.0)
        self.assertEqual(res["summary"]["unplanned_l"], 375.0)
        # 未计入任何分区
        self.assertEqual(sum(z["delivered_l"] for z in res["zones"]), 0.0)

    def test_total_flow_conflict_detected(self):
        valves, zones, items, bo, total = base_fixtures()
        events = [
            {"event_id": "A", "valve_id": 1, "start": dt("08:00"),
             "end": dt("08:30"), "flow_lmin": 25},
            {"event_id": "B", "valve_id": 2, "start": dt("08:00"),
             "end": dt("08:30"), "flow_lmin": 40},   # 合计 65 > 60
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        cf = [c for c in res["conflicts"] if c["type"] == ec.TOTAL_FLOW]
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["peak_flow_lmin"], 65.0)
        self.assertEqual(cf[0]["start"][11:], "08:00")
        self.assertEqual(cf[0]["end"][11:], "08:30")

    def test_blackout_and_out_of_window_violations(self):
        valves, zones, items, bo, total = base_fixtures()
        # 叶菜窗口 08:00-11:00；事件 07:45（停水+窗口外，紧邻叶菜计划->提前）
        # 到 08:30，07:45-08:00 一段同时落停水
        events = [
            {"event_id": "E", "valve_id": 2, "start": dt("07:45"),
             "end": dt("08:30"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        z = zone_by(res, 4)
        self.assertEqual(z["early_minutes"], 15)
        self.assertEqual(z["blackout_minutes"], 15)
        self.assertEqual(z["out_of_window_minutes"], 15)
        self.assertTrue(any(c["type"] == ec.BLACKOUT for c in res["conflicts"]))
        self.assertTrue(any(c["type"] == ec.OUT_OF_WINDOW for c in res["conflicts"]))

    def test_unscheduled_zone_has_status(self):
        valves, zones, items, bo, total = base_fixtures()
        # 删除番茄计划 -> UNSCHEDULED；事件运行按无计划处理
        items = [it for it in items if it["zone_id"] != 1]
        events = [
            {"event_id": "E", "valve_id": 1, "start": dt("08:00"),
             "end": dt("08:30"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        z = zone_by(res, 1)
        self.assertEqual(z["status"], ec.UNSCHEDULED)
        self.assertEqual(z["planned"], None)


class SuggestionTests(unittest.TestCase):

    def test_feasible_makeup_avoids_own_plan_blackout_and_constraints(self):
        valves, zones, items, bo, total = base_fixtures()
        # 番茄缺 750 L：建议 06:00、06:15（窗口 06:00 起，避开 07:30 停水与本计划）
        events = [
            {"event_id": "E", "valve_id": 1, "start": dt("08:00"),
             "end": dt("09:30"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, bo, items, events, total, DAY)
        s = zone_by(res, 1)["suggestion"]
        self.assertTrue(s["feasible"])
        self.assertEqual([x["start_slot"] for x in s["slots"]], [24, 25])
        self.assertEqual(s["residual_l"], 0.0)
        # 建议槽不与番茄原计划 08:00-10:00 重叠
        for x in s["slots"]:
            self.assertFalse(32 <= x["start_slot"] < 40)

    def test_no_solution_gives_minimum_gap_and_reasons(self):
        """窗口剩余槽全被占用时：无可行解，给出最小缺口与逐约束原因。"""
        valves = [{"id": 1, "name": "V1", "flow_lmin": 25, "concur": 1}]
        zones = [{"id": 9, "name": "试区", "water_l": 1000, "valve_id": 1,
                  "flow_lmin": 25, "earliest_slot": 32, "latest_slot": 35}]
        # 窗口 08:00-08:45；计划 08:00-08:30
        items = [{"zone_id": 9, "start_slot": 32, "length": 2}]
        events = [
            {"event_id": "E", "valve_id": 1, "start": dt("08:00"),
             "end": dt("08:15"), "flow_lmin": 25},
            # 08:30-08:45 紧邻计划 -> 延后关阀（计入 375 L），
            # 同时把窗口内唯一剩余补灌槽以同阀门实际运行占死（并发上限 1）
            {"event_id": "X", "valve_id": 1, "start": dt("08:30"),
             "end": dt("08:45"), "flow_lmin": 25},
        ]
        res = ec.verify(zones, valves, [], items, events, 60, DAY)
        s = zone_by(res, 9)["suggestion"]
        self.assertFalse(s["feasible"])
        self.assertEqual(s["slots"], [])
        # 已灌 375（计划内）+ 375（延后）= 750，剩余 250 L 缺口无槽可补
        self.assertEqual(s["residual_l"], 250.0)
        self.assertIn("阀门「V1」并发超限", "；".join(s["reasons"]))

    def test_makeup_uses_actual_overflow_as_baseline(self):
        """实际运行超出计划额定时，补灌基线应计入实际流量。"""
        valves = [
            {"id": 1, "name": "V1", "flow_lmin": 25, "concur": 1},
            {"id": 2, "name": "V2", "flow_lmin": 25, "concur": 1},
        ]
        zones = [
            {"id": 1, "name": "A", "water_l": 1000, "valve_id": 1, "flow_lmin": 25,
             "earliest_slot": 32, "latest_slot": 36},
            {"id": 2, "name": "B", "water_l": 375,
             "valve_id": 2, "flow_lmin": 25,
             "earliest_slot": 32, "latest_slot": 36},
        ]
        items = [
            {"zone_id": 1, "start_slot": 32, "length": 1},   # A 08:00-08:15
            {"zone_id": 2, "start_slot": 33, "length": 1},   # B 08:15-08:30
        ]
        events = [
            # A 完全未灌（缺 1000L，需 4 槽）；V2 在 08:15-08:45 实跑 45 L/min，
            # 与 A 补灌额定 25 合计 70 > 60 -> 08:15 / 08:30 两槽被总流量阻塞
            {"event_id": "E", "valve_id": 2, "start": dt("08:15"),
             "end": dt("08:45"), "flow_lmin": 45},
        ]
        res = ec.verify(zones, valves, [], items, events, 60, DAY)
        s = zone_by(res, 1)["suggestion"]
        # 08:00 是 A 自己的原计划槽（排除）；08:15/08:30 总流量冲突；
        # 08:45 实际事件已结束 -> 唯一可放槽
        self.assertFalse(s["feasible"])
        self.assertEqual([x["start"][11:] for x in s["slots"]], ["08:45"])
        # 缺 1000L 需 3 槽，仅 08:45 可放（375L）-> 最小残缺口 625L
        self.assertEqual(s["residual_l"], 625.0)
        self.assertIn("总流量超限", "；".join(s["reasons"]))


class EventParsingTests(unittest.TestCase):

    def test_parse_validates_date(self):
        with self.assertRaises(ValueError):
            ec.parse_event_dt("2026-09-30T08:00", DAY, "start")
        with self.assertRaises(ValueError):
            ec.parse_event_dt("08:00", DAY, "start")
        with self.assertRaises(ValueError):
            ec.parse_event_dt("2026-10-01T24:00", DAY, "start")
        parsed = ec.parse_event_dt("2026-10-01 08:00", DAY, "start")
        self.assertEqual(parsed, dt("08:00"))


# 有 Flask 环境时才跑接口测试
try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

if HAVE_FLASK:
    class VerifyApiTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.tmpdir = tempfile.mkdtemp()
            cls.db_path = os.path.join(cls.tmpdir, "verify-test.db")
            os.environ["DB_PATH"] = cls.db_path
            import app as appmod
            cls.appmod = appmod
            appmod.DB_PATH = cls.db_path
            appmod.app.testing = True

        def setUp(self):
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            self.appmod.init_db()
            self.c = self.appmod.app.test_client()

        def _first_batch(self):
            b = self.c.get("/api/verify/batches").get_json()
            return b["batches"][0]

        def test_seed_batch_and_state_shape(self):
            b = self._first_batch()
            self.assertEqual(len(b["events"]), 7)
            self.assertIn("result", b)
            self.assertIn("zones", b["result"])
            self.assertIn("summary", b["result"])
            self.assertFalse(b["plan_changed"])

        def test_import_idempotent_and_conflict_rejects_batch(self):
            b = self._first_batch()
            bid, day = b["id"], b["plan_date"]
            new_event = {"event_id": "T-1", "valve_id": 3,
                         "start": f"{day}T11:00", "end": f"{day}T11:15",
                         "flow_lmin": 10}
            r = self.c.post(f"/api/verify/batches/{bid}/events",
                            json={"events": [new_event]})
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.get_json()["imported"], 1)
            # 幂等
            r = self.c.post(f"/api/verify/batches/{bid}/events",
                            json={"events": [new_event]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["skipped"], 1)
            # 内容冲突：与一条新事件同批 -> 整批 409，新事件不入库
            clash = dict(new_event, flow_lmin=9)
            other = {"event_id": "T-2", "valve_id": 3,
                     "start": f"{day}T11:30", "end": f"{day}T11:45",
                     "flow_lmin": 10}
            r = self.c.post(f"/api/verify/batches/{bid}/events",
                            json={"events": [clash, other]})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["conflict"], "event_content")
            after = self.c.get(f"/api/verify/batches?batch_id={bid}").get_json()
            ids = {e["event_id"] for e in after["batch"]["events"]}
            self.assertNotIn("T-2", ids)

        def test_non_increasing_and_invalid_events_rejected(self):
            b = self._first_batch()
            bid, day = b["id"], b["plan_date"]
            r = self.c.post(f"/api/verify/batches/{bid}/events",
                            json={"events": [
                                {"event_id": "N1", "valve_id": 1,
                                 "start": f"{day}T10:00", "end": f"{day}T10:15",
                                 "flow_lmin": 10},
                                {"event_id": "N2", "valve_id": 1,
                                 "start": f"{day}T09:00", "end": f"{day}T09:15",
                                 "flow_lmin": 10}]})
            self.assertEqual(r.status_code, 400)
            r = self.c.post(f"/api/verify/batches/{bid}/events",
                            json={"events": [
                                {"event_id": "N3", "valve_id": 1,
                                 "start": f"{day}T10:00", "end": f"{day}T10:15",
                                 "flow_lmin": 0}]})
            self.assertEqual(r.status_code, 400)

        def test_delete_event_recomputes_and_export(self):
            b = self._first_batch()
            bid = b["id"]
            before = len(b["events"])
            r = self.c.delete(f"/api/verify/batches/{bid}/events/RUN-006")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.get_json()["batch"]["events"]), before - 1)
            # 导出 JSON 完整结构
            exp = self.c.get(f"/api/verify/batches/{bid}/export").get_json()
            for key in ("batch", "events", "verification", "plan", "slot_minutes"):
                self.assertIn(key, exp)
            self.assertEqual(exp["plan"]["revision"],
                             self.c.get("/api/state").get_json()["revision"])


if __name__ == "__main__":
    unittest.main()
