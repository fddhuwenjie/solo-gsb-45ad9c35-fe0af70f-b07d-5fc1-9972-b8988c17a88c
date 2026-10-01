"""用水回溯回归测试：半开状态区间 × 15 分钟灌溉时段。

运行（无需启动服务，纯模块级）：

    python -m unittest test_water_trace -v

覆盖边界：
- 采样时刻起采用新状态（区间左闭右开，槽中切分）；
- 当日首条采样前沿用最近历史状态，无历史样本 -> 证据不足；
- 超标 / 观察 / 正常三类水量分别汇总；
- 阀门未绑定水源 -> 证据不足且不计水量；
- 补录 / 删除采样后按全量重算结果变化；
- 分区筛选与水量 = 流量 × 重叠分钟的数值。
"""

import unittest
from datetime import date

import water_quality as wq
import water_trace as wt

DAY = date(2026, 9, 30)
RANGES = wq.default_ranges()


def sample(sid, ts, ph=7.0, ec=1.0, turb=2.0, nitrate=10.0, source_id=1):
    return {"id": sid, "source_id": source_id, "ts": ts,
            "ph": ph, "ec": ec, "turbidity": turb, "nitrate": nitrate}


def samples_by_source(*rows):
    return {1: list(rows)}, {1: RANGES}


def setup(rows, zones=None, items=None, valves=None):
    sb, rb = samples_by_source(*rows)
    zones = zones or [{"id": 10, "name": "番茄区", "valve_id": 1}]
    items = items or [{"zone_id": 10, "start_slot": 22, "length": 4}]  # 05:30-06:30
    valves = valves or [
        {"id": 1, "name": "中部干管", "flow_lmin": 25, "source_id": 1}]
    return wt.trace_day(zones, items, valves, sb, rb, DAY,
                        source_names={1: "机井"})


class SegmentRuleTests(unittest.TestCase):

    def test_carry_before_first_sample_insufficient_without_history(self):
        # 当日 06:00 才出现全水源第一条采样：05:30-06:00 证据不足
        res = setup([sample(1, "2026-09-30T06:00:00", nitrate=10)])
        row = res["zones"][0]
        # 证据不足段：无状态结论，但仍登记涉及水量以供复核
        self.assertEqual(row["klass"], wt.INSUFFICIENT)
        kinds = [(o["start"][11:], o["end"][11:], o["klass"], o["source_kind"])
                 for o in row["overlaps"]]
        self.assertEqual(kinds, [
            ("05:30", "06:00", wt.INSUFFICIENT, wt.NO_HISTORY),
            ("06:00", "06:30", wt.SAFE, wt.SAMPLE),
        ])
        self.assertEqual(row["insufficient_l"], 25 * 30)
        self.assertEqual(row["safe_l"], 25 * 30)

    def test_carry_uses_latest_history_state(self):
        # 昨日 14:00 已连续两次超标；今日 06:00 仍超标
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),  # 首次越界 -> 观察
            sample(2, "2026-09-29T14:00:00", nitrate=36),  # 连续两次 -> 超标
            sample(3, "2026-09-30T06:00:00", nitrate=35),
        ]
        res = setup(rows)
        row = res["zones"][0]
        kinds = [(o["start"][11:], o["end"][11:], o["klass"], o["source_kind"],
                  o["source_sample_ts"][11:16]) for o in row["overlaps"]]
        self.assertEqual(kinds, [
            ("05:30", "06:00", wt.RISK, wt.CARRY, "14:00"),
            ("06:00", "06:30", wt.RISK, wt.SAMPLE, "06:00"),
        ])
        self.assertEqual(row["risk_l"], 25 * 60)
        self.assertEqual(row["triggers"], ["硝酸盐"])

    def test_state_switches_at_sample_moment(self):
        # 历史超标；今日 06:00 一次回区间 -> 观察：06:00 整点起由风险转待复核
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
            sample(3, "2026-09-30T06:00:00", nitrate=25),
        ]
        res = setup(rows)
        row = res["zones"][0]
        self.assertEqual(row["klass"], wt.RISK)  # 含风险段，整区结论为风险
        self.assertEqual(row["risk_l"], 25 * 30)
        self.assertEqual(row["review_l"], 25 * 30)
        kinds = [(o["start"][11:], o["klass"]) for o in row["overlaps"]]
        self.assertEqual(kinds, [("05:30", wt.RISK), ("06:00", wt.REVIEW)])

    def test_mid_slot_sample_splits_with_minutes(self):
        # 采样 06:07 落在槽中：切分分钟为 37 / 23（允许小数水量）
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
            sample(3, "2026-09-30T06:07:00", nitrate=25),
        ]
        res = setup(rows)
        seg = res["zones"][0]["overlaps"]
        self.assertEqual([o["minutes"] for o in seg], [37.0, 23.0])
        self.assertEqual(seg[0]["water_l"], 25 * 37)
        self.assertEqual(seg[1]["water_l"], 25 * 23)
        self.assertEqual(res["zones"][0]["classified_minutes"], 60.0)

    def test_no_sample_today_uses_history_all_day(self):
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
        ]
        res = setup(rows)
        row = res["zones"][0]
        self.assertEqual(len(row["overlaps"]), 1)
        self.assertEqual(row["overlaps"][0]["source_kind"], wt.CARRY)
        self.assertEqual(row["risk_l"], 25 * 60)

    def test_recovery_to_normal_after_two_in_range(self):
        # 历史超标 -> 今日两次回区间后恢复正常
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
            sample(3, "2026-09-30T05:00:00", nitrate=25),  # 观察
            sample(4, "2026-09-30T05:30:00", nitrate=26),  # 正常
        ]
        # 灌溉 07:00-08:00 应全部正常
        res = setup(rows, items=[{"zone_id": 10, "start_slot": 28, "length": 4}])
        row = res["zones"][0]
        self.assertEqual(row["klass"], wt.SAFE)
        self.assertEqual(row["safe_l"], 25 * 60)


class BindingAndFilterTests(unittest.TestCase):

    def test_unbound_valve_is_insufficient_no_volume(self):
        valves = [{"id": 1, "name": "苗床支管", "flow_lmin": 10, "source_id": None}]
        sb, rb = samples_by_source(sample(1, "2026-09-29T08:00:00"))
        res = wt.trace_day(
            [{"id": 10, "name": "育苗区", "valve_id": 1}],
            [{"zone_id": 10, "start_slot": 28, "length": 3}],
            valves, sb, rb, DAY)
        row = res["zones"][0]
        self.assertEqual(row["klass"], wt.INSUFFICIENT)
        self.assertEqual(row["risk_l"], 0.0)
        self.assertEqual(row["overlaps"][0]["source_kind"], wt.UNBOUND)
        self.assertEqual([v["id"] for v in res["unbound_valves"]], [1])

    def test_zone_filter(self):
        zones = [
            {"id": 10, "name": "番茄区", "valve_id": 1},
            {"id": 11, "name": "黄瓜区", "valve_id": 1},
        ]
        items = [
            {"zone_id": 10, "start_slot": 28, "length": 2},
            {"zone_id": 11, "start_slot": 72, "length": 2},
        ]
        sb, rb = samples_by_source(
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
        )
        valves = [{"id": 1, "name": "干管", "flow_lmin": 25, "source_id": 1}]
        res = wt.trace_day(zones, items, valves, sb, rb, DAY, zone_filter=11)
        self.assertEqual([z["zone_id"] for z in res["zones"]], [11])

    def test_recompute_after_delete_sample(self):
        rows = [
            sample(1, "2026-09-29T08:00:00", nitrate=34),
            sample(2, "2026-09-29T14:00:00", nitrate=36),
            sample(3, "2026-09-30T05:00:00", nitrate=25),  # 观察
            sample(4, "2026-09-30T07:00:00", nitrate=26),  # 恢复正常
        ]
        items = [{"zone_id": 10, "start_slot": 30, "length": 2}]  # 07:30-08:00
        before = setup(rows, items=items)
        self.assertEqual(before["zones"][0]["klass"], wt.SAFE)
        # 删除恢复链中的一条 -> 07:30 只剩一次回归，回落观察
        after = setup([r for r in rows if r["id"] != 4], items=items)
        self.assertEqual(after["zones"][0]["klass"], wt.REVIEW)
        self.assertEqual(after["zones"][0]["review_l"], 25 * 30)

    def test_summary_totals_and_ordering(self):
        # 两分区：一个风险（05:30）、一个绑定观察水源（18:00）
        zones = [
            {"id": 10, "name": "番茄区", "valve_id": 1},
            {"id": 11, "name": "黄瓜区", "valve_id": 2},
        ]
        items = [
            {"zone_id": 10, "start_slot": 32, "length": 4},  # 08:00-09:00
            {"zone_id": 11, "start_slot": 72, "length": 4},  # 18:00-19:00
        ]
        sb = {
            1: [sample(1, "2026-09-29T08:00:00", nitrate=34),
                sample(2, "2026-09-29T14:00:00", nitrate=36)],
            2: [sample(3, "2026-09-29T08:00:00", source_id=2, turb=12)],
        }
        rb = {1: RANGES, 2: RANGES}
        valves = [
            {"id": 1, "name": "北", "flow_lmin": 25, "source_id": 1},
            {"id": 2, "name": "中", "flow_lmin": 25, "source_id": 2},
        ]
        res = wt.trace_day(zones, items, valves, sb, rb, DAY)
        self.assertEqual([z["klass"] for z in res["zones"]],
                         [wt.RISK, wt.REVIEW])  # 风险排前
        self.assertEqual(res["summary"]["risk_l"], 25 * 60)
        self.assertEqual(res["summary"]["review_l"], 25 * 60)
        self.assertEqual(res["summary"]["risk_zones"], [10])

    def test_parse_day_rejects_datetime_string(self):
        for bad in ("2026-9-1", "2026-09-30T08:00", "", None):
            with self.assertRaises(ValueError):
                wt.parse_day(bad)
        self.assertEqual(wt.parse_day("2026-09-30"), DAY)


if __name__ == "__main__":
    unittest.main()
