"""执行核验核心：把阀门实际运行事件与当日 15 分钟轮灌计划逐段对齐核算。

与 scheduler.py 一样只做纯计算，不读写数据库。

事件
----
每条阀门运行事件 = {event_id, valve_id, start, end, flow_lmin}，
start/end 为当日 ``datetime``（分钟精度），区间左闭右开 [start, end)。
同 event_id 同内容幂等；同 event_id 内容不同由应用层整批拒绝。

逐段对齐（半开区间）
--------------------
按阀门分组，取该阀门全部「计划段」（zone_id + [a,b)）与事件区间的所有
端点切分原子小段。对每个事件覆盖到的小段：

- 恰被 **1 条计划段**覆盖（区间交集非空）-> 归属该分区，按
  实际流量 × 时长计水量；
- 被 **≥2 条计划段**覆盖（如计划本身在同一阀门上重叠）-> **证据不足**，
  只登记、**不重复计量**；
- 未被任何计划段覆盖：
    * 与唯一一条计划段紧邻（恰在其开始之前或结束之后）-> 归属该分区，
      记为提前开阀 / 延后关阀（水量仍计入实际灌水）；
    * 夹在两条计划段之间（两侧各触一条）-> 证据不足；
    * 其他 -> 「无计划运行」（不计入任何分区）。

同一阀门事件时间重叠本身即「阀门并发」实际冲突；再跨阀门汇总每分钟
总流量标出总流量冲突；停水时段、分区允许窗口之外的运行逐段标「越界」。

补灌建议（不改原计划）
----------------------
对缺水（唯一归属水量 < 计划需水量，证据不足不计入）的分区，在允许窗口
内、避开本分区原计划段与停水时段后，按 15 分钟槽贪心寻找可行槽：
基线**同时**叠加原计划占用（额定流量 / 并发数）与实际运行占用
（实际流量 / 实际并发表，同分钟取较保守者），再叠加已选补灌槽。
无可行槽时给出「最多能补多少 + 最小缺口 + 逐约束阻塞分钟与原因」。
"""

import math
from datetime import datetime, timedelta

SLOT_MIN = 15
MINUTES_PER_DAY = 24 * 60
SLOTS_PER_DAY = MINUTES_PER_DAY // SLOT_MIN
EPS = 1e-9

# 分区核验结论
OK = "ok"                      # 已灌水量达到计划
DEFICIT = "deficit"            # 缺水
OVER = "over"                  # 超量（归属水量超过计划）
INSUFFICIENT = "insufficient"  # 存在证据不足小段
UNSCHEDULED = "unscheduled"    # 计划中未编排

STATUS_LABEL = {
    OK: "达标", DEFICIT: "缺水", OVER: "超量",
    INSUFFICIENT: "证据不足", UNSCHEDULED: "未编排",
}

# 小段归属类型
SEG_PLANNED = "planned"       # 与唯一计划段相交
SEG_AMBIGUOUS = "ambiguous"   # 多计划段相交，证据不足
SEG_EARLY = "early"           # 计划开始前，紧邻唯一计划段
SEG_LATE = "late"             # 计划结束后，紧邻唯一计划段
SEG_UNPLANNED = "unplanned"   # 无计划运行

SEG_LABEL = {
    SEG_PLANNED: "计划内", SEG_AMBIGUOUS: "证据不足",
    SEG_EARLY: "提前开阀", SEG_LATE: "延后关阀", SEG_UNPLANNED: "无计划运行",
}

CONCURRENCY = "concurrency"      # 阀门并发冲突
TOTAL_FLOW = "total_flow"        # 总流量冲突
BLACKOUT = "blackout"            # 停水运行
OUT_OF_WINDOW = "out_of_window"  # 超出分区允许时段

CONFLICT_LABEL = {
    CONCURRENCY: "阀门并发冲突", TOTAL_FLOW: "总流量超限",
    BLACKOUT: "停水时段运行", OUT_OF_WINDOW: "超出允许灌溉时段",
}


# ---------------------------------------------------------------- 工具

def r2(x):
    """两位小数，归一化 -0.0。"""
    v = round(float(x) + 0.0, 2)
    return 0.0 if v == 0 else v


def day_bounds(day):
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def mm_of_day(day_start, minute):
    return (day_start + timedelta(minutes=minute)).isoformat(timespec="minutes")


def parse_event_dt(raw, day, field):
    """事件时间：'YYYY-MM-DDTHH:MM'（秒可省、空格分隔亦可），必须落在当天。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"事件缺少时间字段：{field}")
    text = raw.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"事件时间格式错误：{raw}（应为 {day.isoformat()}THH:MM）")
    if dt.second or dt.microsecond:
        raise ValueError(f"事件时间需精确到分钟：{raw}")
    day_start, day_end = day_bounds(day)
    if not (day_start <= dt < day_end):
        raise ValueError(f"事件时间不在计划日期 {day.isoformat()} 当天：{raw}")
    return dt


def merge_ranges(flags):
    """bool[1440] -> 连续 True 的分钟区间 [(start_min, end_min_exclusive), ...]。"""
    out, i = [], 0
    while i < MINUTES_PER_DAY:
        if flags[i]:
            j = i
            while j < MINUTES_PER_DAY and flags[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


# ---------------------------------------------------------------- 分钟占用表

class MinuteGrid:
    """全天 1440 分钟的实际运行累计。

    identity_flow[v][m] = 阀门 v 在第 m 分钟唯一运行事件的实际流量；
                          多事件重叠（并发）时记 None，表示流量身份不明。
    flow_sum[v][m]     = 阀门 v 在第 m 分钟全部运行事件的实际流量之和。
    valve_count[v][m]  = 阀门 v 第 m 分钟开启的事件数。
    total_flow[m]      = 第 m 分钟所有事件实际流量之和。
    """

    def __init__(self, valve_ids):
        self.identity_flow = {v: [0.0] * MINUTES_PER_DAY for v in valve_ids}
        self.flow_sum = {v: [0.0] * MINUTES_PER_DAY for v in valve_ids}
        self.valve_count = {v: [0] * MINUTES_PER_DAY for v in valve_ids}
        self.total_flow = [0.0] * MINUTES_PER_DAY

    def add_event(self, event):
        day_start = datetime.combine(event["start"].date(), datetime.min.time())
        a = int((event["start"] - day_start).total_seconds() // 60)
        b = a + int(round((event["end"] - event["start"]).total_seconds() / 60.0))
        f = float(event["flow_lmin"])
        vid = event["valve_id"]
        for m in range(a, b):
            if self.valve_count[vid][m] == 0:
                self.identity_flow[vid][m] = f
            else:
                self.identity_flow[vid][m] = None
            self.flow_sum[vid][m] += f
            self.valve_count[vid][m] += 1
            self.total_flow[m] += f


# ---------------------------------------------------------------- 逐段切分与归属

def classify_segment(seg_a, seg_b, plans):
    """小段 [seg_a, seg_b) 相对该阀门计划段列表的归属。

    返回 (kind, zone_id_or_None, [覆盖到的计划段])。
    plans: [{zone_id, a, b}]，按 (a,b) 排序。
    """
    covered = [p for p in plans if p["a"] < seg_b and seg_a < p["b"]]
    if len(covered) == 1:
        return SEG_PLANNED, covered[0]["zone_id"], covered
    if len(covered) >= 2:
        return SEG_AMBIGUOUS, None, covered

    touch_start = [p for p in plans if p["a"] == seg_b]  # 小段右端即某计划开始
    touch_end = [p for p in plans if p["b"] == seg_a]    # 小段左端即某计划结束
    if len(touch_start) == 1 and not touch_end:
        return SEG_EARLY, touch_start[0]["zone_id"], touch_start
    if len(touch_end) == 1 and not touch_start:
        return SEG_LATE, touch_end[0]["zone_id"], touch_end
    return SEG_UNPLANNED, None, []


def build_segments(valves, zones, items, events, day, blackout_ranges):
    """按阀门把事件切成归属小段，返回 (segments, grid)。

    每个原子小段只生成一条记录（多事件重叠不重复计量）：
    - 小段被 ≥2 条事件覆盖（同阀门多路并发）-> 归属证据不足，即使落在
      计划段内也不判给任何一条事件（无法区分是哪路在灌该分区）；
    - 恰被 1 条事件覆盖 -> 按 classify_segment 与计划段对齐。
    """
    valve_ids = [v["id"] for v in valves]
    grid = MinuteGrid(valve_ids)
    for ev in events:
        grid.add_event(ev)

    zone_by_id = {z["id"]: z for z in zones}
    day_start, _ = day_bounds(day)

    plans_by_valve = {}
    window_cuts_by_valve = {}
    for z in zones:
        ew = day_start + timedelta(minutes=z["earliest_slot"] * SLOT_MIN)
        lw = day_start + timedelta(minutes=z["latest_slot"] * SLOT_MIN)
        window_cuts_by_valve.setdefault(z["valve_id"], set()).update({ew, lw})
    for it in items:
        z = zone_by_id.get(it["zone_id"])
        if z is None:
            continue
        a = day_start + timedelta(minutes=it["start_slot"] * SLOT_MIN)
        b = day_start + timedelta(minutes=(it["start_slot"] + it["length"]) * SLOT_MIN)
        plans_by_valve.setdefault(z["valve_id"], []).append(
            {"zone_id": z["id"], "a": a, "b": b})

    events_by_valve = {}
    for ev in events:
        events_by_valve.setdefault(ev["valve_id"], []).append(ev)

    segments = []
    for vid, evs in events_by_valve.items():
        plans = sorted(plans_by_valve.get(vid, []), key=lambda p: (p["a"], p["b"]))
        evs = sorted(evs, key=lambda e: (e["start"], e["end"], e["event_id"]))
        cuts = set()
        for p in plans:
            cuts.add(p["a"])
            cuts.add(p["b"])
        cuts |= window_cuts_by_valve.get(vid, set())
        for ev in evs:
            cuts.add(ev["start"])
            cuts.add(ev["end"])
        cuts = sorted(cuts)

        for a, b in zip(cuts, cuts[1:]):
            if b <= a:
                continue
            cov = [e for e in evs if e["start"] < b and a < e["end"]]
            if not cov:
                continue  # 该区间没有任何事件运行
            minutes = (b - a).total_seconds() / 60.0
            seg_flow = sum(float(e["flow_lmin"]) for e in cov)
            in_bo = any(ba < b and a < bb for ba, bb in blackout_ranges)

            if len(cov) >= 2:
                # 同阀门多路并发：无法区分哪路事件对应哪条计划，证据不足
                _, _, covered = classify_segment(a, b, plans)
                kind, zid, out_window = SEG_AMBIGUOUS, None, False
            else:
                kind, zid, covered = classify_segment(a, b, plans)
                out_window = False
                if zid is not None:
                    z = zone_by_id[zid]
                    ew = day_start + timedelta(minutes=z["earliest_slot"] * SLOT_MIN)
                    lw = day_start + timedelta(minutes=z["latest_slot"] * SLOT_MIN)
                    out_window = not (ew <= a and b <= lw)

            segments.append({
                "event_ids": [e["event_id"] for e in cov],
                "event_id": cov[0]["event_id"], "valve_id": vid,
                "zone_id": zid, "kind": kind,
                "start": a, "end": b, "minutes": minutes,
                "flow_lmin": seg_flow, "water_l": seg_flow * minutes,
                "covered_zone_ids": sorted({p["zone_id"] for p in covered}),
                "in_blackout": in_bo, "out_of_window": out_window,
            })
    segments.sort(key=lambda s: (s["start"], s["valve_id"], s["event_id"]))
    return segments, grid


# ---------------------------------------------------------------- 分区汇总

def _minute_index(day_start, dt):
    return int((dt - day_start).total_seconds() // 60)


def summarize_zones(zones, items, segments, grid, valves, day,
                    blackout_ranges, total_flow_limit):
    valve_by_id = {v["id"]: v for v in valves}
    item_by_zone = {it["zone_id"]: it for it in items}
    day_start, _ = day_bounds(day)

    seg_by_zone = {}
    for s in segments:
        if s["zone_id"] is not None:
            seg_by_zone.setdefault(s["zone_id"], []).append(s)
    amb_segments = [s for s in segments if s["kind"] == SEG_AMBIGUOUS]
    unplanned_segments = [s for s in segments if s["kind"] == SEG_UNPLANNED]

    zone_rows = []
    for z in zones:
        v = valve_by_id.get(z["valve_id"])
        it = item_by_zone.get(z["id"])
        zsegs = seg_by_zone.get(z["id"], [])

        delivered = sum(s["water_l"] for s in zsegs)
        amb_for_zone = [s for s in amb_segments if z["id"] in s["covered_zone_ids"]]
        ambiguous_l = sum(s["water_l"] for s in amb_for_zone)
        ambiguous_min = sum(s["minutes"] for s in amb_for_zone)
        need = float(z["water_l"])

        early_min = sum(s["minutes"] for s in zsegs if s["kind"] == SEG_EARLY)
        late_min = sum(s["minutes"] for s in zsegs if s["kind"] == SEG_LATE)
        bo_min = sum(s["minutes"] for s in zsegs if s["in_blackout"])
        oow_min = sum(s["minutes"] for s in zsegs if s["out_of_window"])

        interruptions = []
        covered_minutes = 0
        start_delta = end_delta = None
        if it:
            pa = it["start_slot"] * SLOT_MIN
            pb = (it["start_slot"] + it["length"]) * SLOT_MIN

            run_flags = [False] * MINUTES_PER_DAY
            for s in zsegs:
                if s["kind"] != SEG_PLANNED:
                    continue
                sa, sb = _minute_index(day_start, s["start"]), \
                         _minute_index(day_start, s["end"])
                for m in range(max(sa, pa), min(sb, pb)):
                    run_flags[m] = True
            amb_flags = [False] * MINUTES_PER_DAY
            for s in amb_for_zone:
                sa, sb = _minute_index(day_start, s["start"]), \
                         _minute_index(day_start, s["end"])
                for m in range(max(sa, pa), min(sb, pb)):
                    amb_flags[m] = True
            covered_minutes = sum(1 for m in range(pa, pb) if run_flags[m])

            gaps = [m for m in range(pa, pb) if not run_flags[m]]
            i = 0
            while i < len(gaps):
                j = i
                while j + 1 < len(gaps) and gaps[j + 1] == gaps[j] + 1:
                    j += 1
                ga, gb = gaps[i], gaps[j] + 1
                amb_mins = sum(1 for m in range(ga, gb) if amb_flags[m])
                interruptions.append({
                    "start": mm_of_day(day_start, ga),
                    "end": mm_of_day(day_start, gb),
                    "minutes": gb - ga,
                    "ambiguous_minutes": amb_mins,
                    # 纯停机分钟按额定流量折算缺水量；有歧义运行证据的分钟不折算
                    "missed_water_l": r2(float(z["flow_lmin"] or 0) *
                                         (gb - ga - amb_mins)),
                })
                i = j + 1

            if zsegs:
                first = min(s["start"] for s in zsegs)
                last = max(s["end"] for s in zsegs)
                start_delta = int(round(
                    (first - (day_start + timedelta(minutes=pa))).total_seconds() / 60))
                end_delta = int(round(
                    (last - (day_start + timedelta(minutes=pb))).total_seconds() / 60))

        if not it:
            status = UNSCHEDULED
        elif amb_for_zone:
            status = INSUFFICIENT
        elif need - delivered > EPS:
            status = DEFICIT
        elif delivered - need > EPS:
            status = OVER
        else:
            status = OK

        zone_rows.append({
            "zone_id": z["id"], "zone_name": z["name"],
            "valve_id": z["valve_id"],
            "valve_name": v["name"] if v else "（阀门已删除）",
            "planned": None if not it else {
                "start_slot": it["start_slot"],
                "end_slot": it["start_slot"] + it["length"],
                "start": mm_of_day(day_start, it["start_slot"] * SLOT_MIN),
                "end": mm_of_day(
                    day_start, (it["start_slot"] + it["length"]) * SLOT_MIN),
            },
            "status": status,
            "status_label": STATUS_LABEL[status],
            "planned_water_l": r2(need),
            "delivered_l": r2(delivered),
            "deficit_l": r2(max(0.0, need - delivered)),
            "excess_l": r2(max(0.0, delivered - need)),
            "planned_minutes": it["length"] * SLOT_MIN if it else 0,
            "covered_minutes": covered_minutes,
            "interrupted_minutes": sum(g["minutes"] for g in interruptions),
            "ambiguous_minutes": ambiguous_min,
            "ambiguous_l": r2(ambiguous_l),
            "early_minutes": early_min,
            "late_minutes": late_min,
            "blackout_minutes": bo_min,
            "out_of_window_minutes": oow_min,
            "start_delta_min": start_delta,
            "end_delta_min": end_delta,
            "interruptions": interruptions,
            "suggestion": None,
        })

    suggestions = build_suggestions(
        zones, items, valves, grid, day, blackout_ranges, total_flow_limit,
        {row["zone_id"]: row["delivered_l"] for row in zone_rows})
    sug_by_zone = {s["zone_id"]: s for s in suggestions}
    for row in zone_rows:
        row["suggestion"] = sug_by_zone.get(row["zone_id"])

    conflicts = actual_conflicts(
        valves, zones, grid, segments, day, total_flow_limit)

    return {
        "zones": zone_rows,
        "conflicts": conflicts,
        "ambiguous_segments": [seg_dict(s, day_start) for s in amb_segments],
        "unplanned_segments": [seg_dict(s, day_start) for s in unplanned_segments],
    }


def seg_dict(s, day_start):
    return {
        "event_ids": list(s.get("event_ids", [s["event_id"]])),
        "event_id": s["event_id"], "valve_id": s["valve_id"],
        "zone_id": s["zone_id"], "kind": s["kind"],
        "kind_label": SEG_LABEL[s["kind"]],
        "start": s["start"].isoformat(timespec="minutes"),
        "end": s["end"].isoformat(timespec="minutes"),
        "minutes": r2(s["minutes"]), "flow_lmin": r2(s["flow_lmin"]),
        "water_l": r2(s["water_l"]),
        "covered_zone_ids": s["covered_zone_ids"],
        "in_blackout": s["in_blackout"],
        "out_of_window": s["out_of_window"],
        "_start_min": _minute_index(day_start, s["start"]),
        "_end_min": _minute_index(day_start, s["end"]),
    }


# ---------------------------------------------------------------- 实际冲突

def actual_conflicts(valves, zones, grid, segments, day, total_flow_limit):
    day_start, _ = day_bounds(day)
    zone_by_id = {z["id"]: z for z in zones}
    out = []

    for v in valves:
        flags = [grid.valve_count[v["id"]][m] > int(v["concur"])
                 for m in range(MINUTES_PER_DAY)]
        for a, b in merge_ranges(flags):
            peak = max(grid.valve_count[v["id"]][a:b])
            out.append({
                "type": CONCURRENCY, "type_label": CONFLICT_LABEL[CONCURRENCY],
                "valve_id": v["id"], "valve_name": v["name"],
                "start": mm_of_day(day_start, a), "end": mm_of_day(day_start, b),
                "minutes": b - a, "peak_count": peak,
                "detail": f"阀门「{v['name']}」同时 {peak} 路运行"
                          f"（上限 {int(v['concur'])}）",
            })

    if total_flow_limit is not None:
        flags = [grid.total_flow[m] > float(total_flow_limit) + EPS
                 for m in range(MINUTES_PER_DAY)]
        for a, b in merge_ranges(flags):
            peak = max(grid.total_flow[a:b])
            out.append({
                "type": TOTAL_FLOW, "type_label": CONFLICT_LABEL[TOTAL_FLOW],
                "start": mm_of_day(day_start, a),
                "end": mm_of_day(day_start, b),
                "minutes": b - a, "peak_flow_lmin": r2(peak),
                "limit_lmin": r2(total_flow_limit),
                "detail": f"实际总流量峰值 {r2(peak)} > {r2(total_flow_limit)} L/min",
            })

    bo_flags = [False] * MINUTES_PER_DAY
    oow_flags = [False] * MINUTES_PER_DAY
    oow_zone = {}
    for s in segments:
        sa, sb = _minute_index(day_start, s["start"]), \
                 _minute_index(day_start, s["end"])
        if s["in_blackout"]:
            for m in range(sa, sb):
                bo_flags[m] = True
        if s["out_of_window"]:
            for m in range(sa, sb):
                oow_flags[m] = True
                oow_zone.setdefault(m, s["zone_id"])
    for a, b in merge_ranges(bo_flags):
        out.append({
            "type": BLACKOUT, "type_label": CONFLICT_LABEL[BLACKOUT],
            "start": mm_of_day(day_start, a), "end": mm_of_day(day_start, b),
            "minutes": b - a, "detail": "停水时段内有阀门实际运行",
        })
    for a, b in merge_ranges(oow_flags):
        zid = oow_zone.get(a)
        zname = zone_by_id[zid]["name"] if zid in zone_by_id else None
        out.append({
            "type": OUT_OF_WINDOW, "type_label": CONFLICT_LABEL[OUT_OF_WINDOW],
            "zone_id": zid, "zone_name": zname,
            "start": mm_of_day(day_start, a), "end": mm_of_day(day_start, b),
            "minutes": b - a,
            "detail": (f"分区「{zname}」实际运行超出允许灌溉时段"
                       if zname else "实际运行超出分区允许灌溉时段"),
        })

    out.sort(key=lambda r: (r["start"], r["type"]))
    return out


# ---------------------------------------------------------------- 补灌建议

def build_suggestions(zones, items, valves, grid, day, blackout_ranges,
                      total_flow_limit, delivered_by_zone):
    """为缺水（且证据充分）的分区在不改原计划的前提下寻找补灌槽。

    基线每分钟占用：
    - 原计划：额定流量 / 所属阀门并发数（计划自身冲突同样计入）；
    - 实际运行：同分钟同阀门的实际流量高于计划额定时补足差额，
      并发数取计划与实际的较大者；
    - 再逐槽叠加贪心已选的补灌槽。
    候选槽必须：位于允许窗口、不落停水、不与本分区原计划重叠。
    """
    valve_by_id = {v["id"]: v for v in valves}
    zone_by_id = {z["id"]: z for z in zones}
    item_by_zone = {it["zone_id"]: it for it in items}
    day_start, _ = day_bounds(day)

    plan_flow = [0.0] * MINUTES_PER_DAY
    plan_valve = {v["id"]: [0] * MINUTES_PER_DAY for v in valves}
    own_flags = {z["id"]: [False] * MINUTES_PER_DAY for z in zones}
    for it in items:
        z = zone_by_id.get(it["zone_id"])
        if z is None:
            continue
        a, b = it["start_slot"] * SLOT_MIN, \
            (it["start_slot"] + it["length"]) * SLOT_MIN
        for m in range(a, b):
            plan_flow[m] += float(z["flow_lmin"] or 0)
            plan_valve[z["valve_id"]][m] += 1
            own_flags[z["id"]][m] = True

    def planned_flow_at(valve_id, minute):
        """该分钟某阀门上所有原计划段的额定流量之和。"""
        total = 0.0
        for pit in items:
            pz = zone_by_id.get(pit["zone_id"])
            if pz is None or pz["valve_id"] != valve_id:
                continue
            if pit["start_slot"] * SLOT_MIN <= minute < \
                    (pit["start_slot"] + pit["length"]) * SLOT_MIN:
                total += float(pz["flow_lmin"] or 0)
        return total

    extra_flow = [0.0] * MINUTES_PER_DAY
    for v in valves:
        for m in range(MINUTES_PER_DAY):
            if grid.valve_count[v["id"]][m] <= 0:
                continue
            planned_here = planned_flow_at(v["id"], m)
            # 并发分钟身份不明时，以该阀门实际流量合计作为保守实际占用
            ident = grid.identity_flow[v["id"]][m]
            actual_here = max(grid.flow_sum[v["id"]][m],
                              ident if ident is not None else 0.0)
            if actual_here > planned_here:
                extra_flow[m] += actual_here - planned_here

    bo_flags = [False] * MINUTES_PER_DAY
    for ba, bb in blackout_ranges:
        a = max(0, int((ba - day_start).total_seconds() // 60))
        b = min(MINUTES_PER_DAY, int((bb - day_start).total_seconds() // 60))
        for m in range(a, b):
            bo_flags[m] = True

    suggestions = []
    for z in zones:
        it = item_by_zone.get(z["id"])
        v = valve_by_id.get(z["valve_id"])
        if not it or not v:
            continue
        deficit = float(z["water_l"]) - float(delivered_by_zone.get(z["id"], 0.0))
        if deficit <= EPS:
            continue  # 证据不足导致的状态由分区行体现，未确认缺水不出建议

        ew, lw = z["earliest_slot"] * SLOT_MIN, z["latest_slot"] * SLOT_MIN
        rate = float(v["flow_lmin"])
        need_slots = max(1, math.ceil(deficit / (rate * SLOT_MIN)))

        used_flow = [0.0] * MINUTES_PER_DAY
        used_valve = [0] * MINUTES_PER_DAY
        picked, blocked = [], {}

        def slot_reason(slot):
            """返回该槽不可行的约束原因（逐分钟按优先级判定）；slot 为槽号。

            与本分区原计划重叠的槽不属于「剩余允许时段」，直接跳过、不计阻塞。
            返回 None 表示可行，返回 "skip" 表示原计划槽（跳过不统计）。
            """
            m0, m1 = slot * SLOT_MIN, (slot + 1) * SLOT_MIN
            if any(own_flags[z["id"]][m] for m in range(m0, m1)):
                return "skip"
            for m in range(m0, m1):
                if bo_flags[m]:
                    return "停水时段"
                base_flow = plan_flow[m] + extra_flow[m] + used_flow[m]
                if total_flow_limit is not None and \
                        base_flow + rate > float(total_flow_limit) + EPS:
                    return (f"总流量超限（{r2(base_flow + rate)} > "
                            f"{r2(total_flow_limit)} L/min）")
                base_v = max(plan_valve[v["id"]][m],
                             grid.valve_count[v["id"]][m]) + used_valve[m]
                if base_v + 1 > int(v["concur"]):
                    return f"阀门「{v['name']}」并发超限（>{int(v['concur'])}）"
            return None

        for slot in range(ew // SLOT_MIN, lw // SLOT_MIN):
            if len(picked) >= need_slots:
                break
            reason = slot_reason(slot)
            if reason is None:
                picked.append(slot)
                for m in range(slot * SLOT_MIN, slot * SLOT_MIN + SLOT_MIN):
                    used_flow[m] += rate
                    used_valve[m] += 1
            elif reason != "skip":
                key = reason.split("（")[0]
                blocked[key] = blocked.get(key, 0) + SLOT_MIN

        can_fill = len(picked) * rate * SLOT_MIN
        residual = max(0.0, deficit - can_fill)
        suggestions.append({
            "zone_id": z["id"],
            "deficit_l": r2(deficit),
            "need_slots": need_slots,
            "feasible": residual <= EPS,
            "slots": [{
                "start_slot": s, "end_slot": s + 1,
                "start": mm_of_day(day_start, s * SLOT_MIN),
                "end": mm_of_day(day_start, (s + 1) * SLOT_MIN),
                "water_l": r2(rate * SLOT_MIN),
            } for s in picked],
            "suppliable_l": r2(min(deficit, can_fill)),
            "residual_l": r2(residual),
            "blocked_minutes": blocked,
            "reasons": sorted(blocked, key=lambda k: -blocked[k]),
        })
    return suggestions


# ---------------------------------------------------------------- 全量核验

def verify(zones, valves, blackouts, items, events, total_flow_limit, day):
    """对一批事件与当日计划做全量核验（纯计算）。

    zones:  [{id,name,water_l,valve_id,earliest_slot,latest_slot,flow_lmin}]
    events: [{event_id,valve_id,start(datetime),end(datetime),flow_lmin}]
    """
    day_start, _ = day_bounds(day)
    blackout_ranges = [
        (day_start + timedelta(minutes=b["start_slot"] * SLOT_MIN),
         day_start + timedelta(minutes=b["end_slot"] * SLOT_MIN))
        for b in blackouts
    ]
    segments, grid = build_segments(valves, zones, items, events, day,
                                    blackout_ranges)
    result = summarize_zones(zones, items, segments, grid, valves, day,
                             blackout_ranges, total_flow_limit)

    result["summary"] = {
        "date": day.isoformat(),
        "event_count": len(events),
        "zone_count": len(zones),
        "delivered_l": r2(sum(r0["delivered_l"] for r0 in result["zones"])),
        "deficit_l": r2(sum(r0["deficit_l"] for r0 in result["zones"])),
        "ambiguous_l": r2(sum(s["water_l"] for s in result["ambiguous_segments"])),
        "unplanned_l": r2(sum(s["water_l"] for s in result["unplanned_segments"])),
        "conflict_count": len(result["conflicts"]),
        "deficit_zones": [r0["zone_id"] for r0 in result["zones"]
                          if r0["status"] == DEFICIT],
        "insufficient_zones": [r0["zone_id"] for r0 in result["zones"]
                               if r0["status"] == INSUFFICIENT],
        "unplanned_event_count": len(
            {eid for s in result["unplanned_segments"] for eid in s["event_ids"]}),
    }
    # 序列化前去掉内部分钟索引
    for key in ("ambiguous_segments", "unplanned_segments"):
        for s in result[key]:
            s.pop("_start_min", None)
            s.pop("_end_min", None)
    return result
