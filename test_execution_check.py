"""执行核验回归测试：事件半开对齐、归属计量、冲突、补灌建议与幂等导入。

运行（无需启动服务，纯模块级）：

    python -m unittest test_execution_check -v

覆盖：
- 校验：时间必须在计划日期当天、分钟对齐、本地时间；批内按时间递增；
- 切分：唯一归属 / 计划外（提前 · 延后 · 中间间隔）/ 证据不足不重复计量；
- 核算：已灌取并集（并发不重复计）、缺水 / 超灌 / 提前延后 / 中断；
- 冲突：阀门并发、总流量超限及区间合并；
- 补灌：剩余允许时段贪心可行、停水 / 并发约束、无解最小缺口与原因；
- 幂等：同编号同内容跳过，内容冲突整批拒绝。
"""

import unittest
from datetime import date

import execution_check as ec

DAY = date(2026, 10, 1)


def E(eid, vid, start, end, flow=25.0):
    return {"event_id": eid, "valve_id": vid,
            "start": f"2026-10-01T{start}", "end": f"2026-10-01T{end}",
            "flow_lmin": flow}


def ER(eid, vid, start, end, flow=25.0):
    """完整时间戳版本（用于跨日端点 / 非计划日期的拒绝用例）。"""
    return {"event_id": eid, "valve_id": vid, "start": start, "end": end,
            "flow_lmin": flow}


def norm(events):
    return ec.validate_events(events, DAY, {1, 2, 3})


def valves():
    return [
        {"id": 1, "name": "北侧干管", "flow_lmin": 25, "concur": 1},
        {"id": 2, "name": "中部干管", "flow_lmin": 25, "concur": 1},
        {"id": 3, "name": "苗床支管", "flow_lmin": 10, "concur": 1},
    ]


def zones():
    return [
        {"id": 1, "name": "番茄区", "water_l": 3000, "valve_id": 1,
         "flow_lmin": 25, "earliest_slot": 24, "latest_slot": 40},
        {"id": 2, "name": "黄瓜区", "water_l": 2250, "valve_id": 2,
         "flow_lmin": 25, "earliest_slot": 72, "latest_slot": 88},
        {"id": 3, "name": "草莓区", "water_l": 1500, "valve_id": 2,
         "flow_lmin": 25, "earliest_slot": 22, "latest_slot": 34},
        {"id": 4, "name": "叶菜区", "water_l": 750,
         "valve_id": 2, "flow_lmin": 25, "earliest_slot": 32,
         "latest_slot": 44},
        {"id": 5, "name": "育苗区", "water_l": 450, "valve_id": 3,
         "flow_lmin": 10, "earliest_slot": 28, "latest_slot": 36},
    ]


def items():
    return [
        {"zone_id": 5, "start_slot": 28, "length": 3},   # 07:00-07:45
        {"zone_id": 1, "start_slot": 32, "length": 8},   # 08:00-10:00
        {"zone_id": 3, "start_slot": 22, "length": 10},  # 05:30-08:00
        {"zone_id": 2, "start_slot": 72, "length": 6},   # 18:00-19:30
        {"zone_id": 4, "start_slot": 40, "length": 2},   # 10:00-10:30
    ]


BLACKOUTS = [{"start_slot": 30, "end_slot": 32}]  # 07:30-08:00


def check(events, total=60, blackouts=None):
    return ec.check_day(zones(), valves(), items(),
                        blackouts if blackouts is not None else BLACKOUTS,
                        norm(events), DAY, total)


class ValidationTests(unittest.TestCase):

    def test_valid_events_sorted(self):
        evs = norm([E("a", 1, "06:00", "07:00"),
                    E("b", 1, "07:00", "08:00")])
        self.assertEqual([e["event_id"] for e in evs], ["a", "b"])

    def test_same_start_allowed_for_concurrent_valves(self):
        evs = norm([E("a", 1, "06:00", "07:00"),
                    E("b", 2, "06:00", "06:30")])
        self.assertEqual(len(evs), 2)

    def test_rejects_unsorted_batch(self):
        with self.assertRaisesRegex(ValueError, "递增"):
            norm([E("b", 1, "08:00", "09:00"),
                  E("a", 1, "07:00", "07:30")])

    def test_rejects_bad_day(self):
        with self.assertRaisesRegex(ValueError, "计划日期"):
            norm([ER("a", 1, "2026-10-02T08:00", "2026-10-02T09:00")])

    def test_rejects_seconds(self):
        with self.assertRaisesRegex(ValueError, "精确到分钟"):
            norm([ER("a", 1, "2026-10-01T08:00:30",
                     "2026-10-01T09:00:00")])

    def test_rejects_timezone(self):
        with self.assertRaisesRegex(ValueError, "时区"):
            norm([ER("a", 1, "2026-10-01T08:00:00Z",
                     "2026-10-01T09:00:00Z")])

    def test_rejects_unknown_valve(self):
        with self.assertRaisesRegex(ValueError, "阀门不存在"):
            norm([E("a", 9, "08:00", "09:00")])

    def test_rejects_end_before_start(self):
        with self.assertRaisesRegex(ValueError, "结束时间"):
            norm([E("a", 1, "09:00", "08:00")])

    def test_end_at_2400_allowed(self):
        evs = norm([ER("a", 1, "2026-10-01T23:30",
                       "2026-10-02T00:00")])
        self.assertEqual(evs[0]["end_min"], 1440)

    def test_duplicate_id_in_batch_rejected(self):
        with self.assertRaisesRegex(ValueError, "event_id 重复"):
            norm([E("a", 1, "06:00", "07:00"),
                  E("a", 1, "08:00", "09:00")])


class IdempotencyTests(unittest.TestCase):

    def setUp(self):
        self.existing = norm([E("a", 1, "08:00", "09:00", 25)])

    def test_same_content_is_idempotent(self):
        merged, ins, skip = ec.merge_with_existing(
            norm([E("a", 1, "08:00", "09:00", 25)]), self.existing)
        self.assertEqual((ins, skip, len(merged)), (0, 1, 1))

    def test_conflicting_content_rejects_whole_batch(self):
        with self.assertRaisesRegex(ValueError, "内容不同"):
            ec.merge_with_existing(
                norm([E("a", 1, "08:00", "09:05", 25)]), self.existing)
        with self.assertRaisesRegex(ValueError, "内容不同"):
            ec.merge_with_existing(
                norm([E("a", 1, "08:00", "09:00", 24)]), self.existing)
        with self.assertRaisesRegex(ValueError, "内容不同"):
            ec.merge_with_existing(
                norm([E("a", 2, "08:00", "09:00", 25)]), self.existing)

    def test_new_event_inserts(self):
        merged, ins, skip = ec.merge_with_existing(
            norm([E("b", 1, "09:00", "10:00", 25)]), self.existing)
        self.assertEqual((ins, skip, len(merged)), (1, 0, 2))


class AlignmentTests(unittest.TestCase):

    def test_full_attribution(self):
        res = check([E("t", 1, "08:00", "10:00")])
        z = next(z for z in res["zones"] if z["zone_id"] == 1)
        self.assertEqual(z["status"], ec.ENOUGH)
        self.assertEqual(z["delivered_l"], 3000)
        self.assertEqual(z["deficit_l"], 0)
        self.assertEqual(z["interrupt_min"], 0)

    def test_interruption_and_deficit(self):
        # 08:00-09:50：中断 10 分钟，缺水 250 L
        res = check([E("t", 1, "08:00", "09:50")])
        z = next(z for z in res["zones"] if z["zone_id"] == 1)
        self.assertEqual(z["status"], ec.DEFICIT)
        self.assertEqual(z["delivered_l"], 2750)
        self.assertEqual(z["deficit_l"], 250)
        self.assertEqual(z["interrupt_min"], 10)

    def test_surplus_within_plan(self):
        # 计划 08:00-10:00 内以大流量运行 -> 归属水量超灌
        res = check([E("t", 1, "08:00", "10:00", 28)])
        z = next(z for z in res["zones"] if z["zone_id"] == 1)
        self.assertEqual(z["status"], ec.SURPLUS)
        self.assertEqual(z["surplus_l"], 360)

    def test_late_suffix_water_is_out_of_plan(self):
        # 超计划结束的 30 分钟归入延后 / 计划外，不计超灌
        res = check([E("t", 1, "08:00", "10:30", 25)])
        z = next(z for z in res["zones"] if z["zone_id"] == 1)
        self.assertEqual(z["status"], ec.ENOUGH)
        self.assertEqual(z["late_min"], 30)
        self.assertEqual(z["out_of_plan_l"], 750)

    def test_early_prefix_out_of_plan(self):
        # 草莓 05:30 计划，04:00 开 -> 04:00-05:30 计划外提前
        res = check([E("s", 2, "04:00", "08:00")])
        z = next(z for z in res["zones"] if z["zone_id"] == 3)
        self.assertEqual(z["early_min"], 90)
        self.assertEqual(z["out_of_plan_l"], 2250)
        # 计划内归属 05:30-08:00 = 3750 L，高于目标 1500 L（计划槽本身多排）
        self.assertEqual(z["delivered_l"], 3750)
        self.assertEqual(z["status"], ec.SURPLUS)
        self.assertEqual(z["surplus_l"], 2250)
        out = [s for s in res["out_of_plan_segments"]
               if s["event_id"] == "s"]
        self.assertEqual([(s["start"], s["end"], s["adjacency"]) for s in out],
                         [("04:00", "05:30", ec.PREFIX)])

    def test_late_suffix_out_of_plan(self):
        # 黄瓜 18:00-19:30 计划，运行到 19:40 -> 延后 10 min
        res = check([E("c", 2, "18:00", "19:40")])
        z = next(z for z in res["zones"] if z["zone_id"] == 2)
        self.assertEqual(z["late_min"], 10)
        self.assertEqual(z["out_of_plan_l"], 250)
        out = [s for s in res["out_of_plan_segments"] if s["event_id"] == "c"]
        self.assertEqual(out[0]["adjacency"], ec.SUFFIX)

    def test_detached_out_of_plan(self):
        # 中部干管在 12:00 运行，不接任何该阀门计划
        res = check([E("x", 2, "12:00", "12:30")])
        out = res["out_of_plan_segments"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["adjacency"], ec.DETACHED)
        self.assertEqual(out[0]["water_l"], 750)

    def test_gap_between_two_plans(self):
        # 草莓 05:30-08:00 与叶菜 10:00-10:30 同阀门；09:30-10:00
        # 既贴叶菜前缀又在草莓之后但不接草莓（08:00 != 09:30）-> 仅 prefix
        res = check([E("g", 2, "09:30", "10:30")])
        out = [s for s in res["out_of_plan_segments"] if s["event_id"] == "g"]
        self.assertEqual([(s["start"], s["end"], s["adjacency"]) for s in out],
                         [("09:30", "10:00", ec.PREFIX)])
        # 夹在两计划之间（端点同时相接）的 gap：构造 08:00 开始的事件，
        # 08:00 贴草莓 suffix，10:00 贴叶菜 prefix 同一段才是 gap
        res2 = check([E("g2", 2, "08:00", "10:30")])
        out2 = [s for s in res2["out_of_plan_segments"] if s["event_id"] == "g2"]
        self.assertEqual([(s["start"], s["end"], s["adjacency"]) for s in out2],
                         [("08:00", "10:00", ec.GAP)])

    def test_ambiguous_not_double_counted(self):
        # 同阀门并发上限 2，两个计划在 06:00-06:30 重叠
        vs = [{"id": 1, "name": "V", "flow_lmin": 25, "concur": 2}]
        zs = [
            {"id": 1, "name": "A", "water_l": 1000, "valve_id": 1,
             "flow_lmin": 25, "earliest_slot": 24, "latest_slot": 28},
            {"id": 2, "name": "B", "water_l": 1000, "valve_id": 1,
             "flow_lmin": 25, "earliest_slot": 24, "latest_slot": 30},
        ]
        its = [{"zone_id": 1, "start_slot": 24, "length": 2},
               {"zone_id": 2, "start_slot": 24, "length": 4}]
        evs = norm([E("a", 1, "06:00", "07:00", 25)])
        res = ec.check_day(zs, vs, its, [], evs, DAY, 1000)
        amb = res["ambiguous_segments"]
        self.assertEqual([(s["start"], s["end"]) for s in amb],
                         [("06:00", "06:30")])
        za = next(z for z in res["zones"] if z["zone_id"] == 1)
        zb = next(z for z in res["zones"] if z["zone_id"] == 2)
        # A 无任何唯一归属段（其整个计划都在重叠里）
        self.assertEqual(za["delivered_l"], 0)
        self.assertEqual(za["deficit_l"], 1000)
        # B 仅有 06:30-07:00 唯一归属
        self.assertEqual(zb["delivered_l"], 750)
        # 证据不足水量登记但不计入任何分区
        self.assertEqual(res["summary"]["ambiguous_l"], 750)

    def test_overlapping_events_union_water(self):
        # 同分区两个重叠事件：并集分钟取最大流量，不重复计量
        vs = [{"id": 1, "name": "V", "flow_lmin": 25, "concur": 2}]
        zs = [{"id": 1, "name": "A", "water_l": 1000, "valve_id": 1,
               "flow_lmin": 25, "earliest_slot": 24, "latest_slot": 32}]
        its = [{"zone_id": 1, "start_slot": 24, "length": 8}]
        evs = norm([
            E("a", 1, "06:00", "07:00", 25),
            E("b", 1, "06:30", "07:30", 30),
        ])
        res = ec.check_day(zs, vs, its, [], evs, DAY, 1000)
        z = res["zones"][0]
        # 06:00-06:30 = 25*30=750；06:30-07:30 并集 max=30*60=1800
        self.assertEqual(z["delivered_l"], 2550)


class ConflictTests(unittest.TestCase):

    def test_valve_concurrency_conflict(self):
        res = check([
            E("y1", 3, "07:00", "07:45", 10),
            E("y2", 3, "07:20", "07:40", 10),
        ])
        vc = [c for c in res["conflicts"] if c["type"] == "valve_concur"]
        self.assertEqual(len(vc), 1)
        self.assertEqual((vc[0]["start"], vc[0]["end"], vc[0]["count"]),
                         ("07:20", "07:40", 2))
        self.assertEqual(vc[0]["event_ids"], ["y1", "y2"])

    def test_total_flow_conflict(self):
        # 08:00-08:30 番茄 25（计划内）再加中部干管 50 -> 75 > 60
        res = check([
            E("t", 1, "08:00", "10:00", 25),
            E("x", 2, "08:00", "08:30", 50),
        ])
        tf = [c for c in res["conflicts"] if c["type"] == "total_flow"]
        self.assertEqual(len(tf), 1)
        self.assertEqual((tf[0]["start"], tf[0]["end"], tf[0]["flow_lmin"]),
                         ("08:00", "08:30", 75))

    def test_adjacent_conflict_spans_merge(self):
        # y2 横跨 07:20-07:40，y3 同时开启 07:30-07:35：
        # 07:20-07:30 与 07:30-07:40 事件集合相同（y1+y2）且端点相接 -> 合并
        res = check([
            E("y1", 3, "07:00", "07:45", 10),
            E("y2", 3, "07:20", "07:40", 10),
        ])
        vc = [c for c in res["conflicts"] if c["type"] == "valve_concur"]
        self.assertEqual(len(vc), 1)
        self.assertEqual((vc[0]["start"], vc[0]["end"]),
                         ("07:20", "07:40"))

    def test_touching_events_form_separate_conflicts(self):
        # y2 07:20-07:30 与 y3 07:30-07:40 仅端点相接（半开区间不重叠）
        # -> 两段冲突，事件集合不同
        res = check([
            E("y1", 3, "07:00", "07:45", 10),
            E("y2", 3, "07:20", "07:30", 10),
            E("y3", 3, "07:30", "07:40", 10),
        ])
        vc = [c for c in res["conflicts"] if c["type"] == "valve_concur"]
        self.assertEqual(len(vc), 2)
        self.assertEqual([(c["start"], c["end"]) for c in vc],
                         [("07:20", "07:30"), ("07:30", "07:40")])


class MakeupTests(unittest.TestCase):

    def test_makeup_fills_deficit_in_free_window(self):
        # 番茄缺 250 L（1 槽），剩余允许窗口 06:00 起
        res = check([E("t", 1, "08:00", "09:50")])
        m = next(m for m in res["makeup"] if m["zone_id"] == 1)
        self.assertTrue(m["feasible"])
        self.assertEqual(m["blocks"],
                         [{"start_slot": 24, "end_slot": 25,
                           "start": "06:00", "end": "06:15",
                           "duration_min": 15}])
        self.assertEqual(m["remaining_l"], 0)

    def test_makeup_respects_blackout(self):
        # 育苗区缺水：允许窗口 07:00-09:00，计划 07:00-07:45；
        # 07:30-08:00 停水 -> 第一个可用槽从 08:00 开始
        vs = valves()
        evs = norm([E("y", 3, "07:00", "07:30", 10)])  # 缺 150 L
        res = ec.check_day(zones(), vs, items(), BLACKOUTS, evs, DAY, 60)
        m = next(m for m in res["makeup"] if m["zone_id"] == 5)
        self.assertTrue(m["feasible"])
        self.assertEqual(m["blocks"][0]["start"], "08:00")
        bo = next((b for b in m["blocked"] if "停水" in b["reason"]), None)
        self.assertIsNotNone(bo)
        # 允许窗口内 07:45-08:00 仅 1 个槽（07:45-08:00）落在停水区间
        self.assertEqual(bo["slot_count"], 1)

    def test_makeup_unsolvable_minimum_gap(self):
        # 番茄缺水且允许窗口内除原计划槽外全部停水：无解 -> 最小缺口
        zs = [dict(z) for z in zones()]
        zs[0]["earliest_slot"] = 32   # 08:00
        zs[0]["latest_slot"] = 40     # 10:00，窗口恰好等于计划槽
        evs = norm([E("t", 1, "08:00", "09:00")])  # 缺 1500
        res = ec.check_day(zs, valves(), items(),
                           [{"start_slot": 24, "end_slot": 32}], evs, DAY, 60)
        m = next(m for m in res["makeup"] if m["zone_id"] == 1)
        self.assertFalse(m["feasible"])
        self.assertEqual(m["free_slot_count"], 0)
        self.assertEqual(m["remaining_l"], 1500)
        self.assertIn("没有", m["reason"])

    def test_makeup_blocked_by_plan_concurrency(self):
        # 叶菜缺水 1 槽；把总流量调为 25，使叶菜补灌（+25）在番茄计划槽上
        # （北侧 25）超限：08:00-10:00 全部因总流量（叠加原计划）不可用，
        # 10:30-11:00 仍可用 -> 建议落在 10:30
        evs = norm([
            E("t", 1, "08:00", "10:00", 25),
            E("l", 2, "10:00", "10:20"),
        ])
        res = ec.check_day(zones(), valves(), items(), BLACKOUTS, evs, DAY, 25)
        m = next(m for m in res["makeup"] if m["zone_id"] == 4)
        self.assertTrue(m["feasible"])
        self.assertEqual(m["blocks"][0]["start"], "10:30")
        blocked = [b for b in m["blocked"]
                   if "总流量超限（叠加原计划占用）" in b["reason"]]
        self.assertTrue(blocked)

    def test_makeup_does_not_modify_plan(self):
        # 重算结果中的原计划 items 不被建议影响（第二次计算结果一致）
        res1 = check([E("t", 1, "08:00", "09:50")])
        res2 = check([E("t", 1, "08:00", "09:50")])
        self.assertEqual(
            [(z["zone_id"], z["plan_start"], z["plan_end"])
             for z in res1["zones"]],
            [(z["zone_id"], z["plan_start"], z["plan_end"])
             for z in res2["zones"]])


class SummaryTests(unittest.TestCase):

    def test_summary_aggregates(self):
        res = check([
            E("y1", 3, "07:00", "07:45", 10),
            E("y2", 3, "07:20", "07:40", 10),  # 并发冲突
            E("t", 1, "08:00", "09:50"),       # 番茄缺 250
        ])
        s = res["summary"]
        self.assertEqual(s["event_count"], 3)
        self.assertIn(1, s["deficit_zones"])   # 番茄明确缺水
        self.assertEqual(s["conflict_count"], 1)
        # 番茄缺 250 L 在剩余允许窗口可补足
        tomato = next(m for m in res["makeup"] if m["zone_id"] == 1)
        self.assertTrue(tomato["feasible"])
        self.assertEqual(tomato["remaining_l"], 0)


if __name__ == "__main__":
    unittest.main()
