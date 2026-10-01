"""执行核验核心：按计划日期把阀门实际运行事件与轮灌计划逐段对齐核算。

系统以**计划日期**为单位保存核验批次，每个批次导入若干阀门运行事件
（event_id 唯一）。本模块是纯计算模块，不读写数据库。

事件对齐规则（半开区间）
------------------------
事件为分钟精度的左闭右开区间 [start, end)；计划项为 15 分钟槽位的
左闭右开区间 [start_slot*15, (start_slot+length)*15)。

对每个事件，取其**同阀门**全部计划项，在所有计划项端点处把事件切分为
若干连续子段，逐段判定归属：

- 恰好落在 1 个计划项内   -> 归属该计划分区（attributed）；
- 不与任何计划项相交       -> 计划外运行（out_of_plan）；
- 同时与 ≥2 个计划项相交  -> 证据不足（ambiguous，无法唯一归属），
                             列出候选分区，**不重复计量**。

与任何计划项都不相交的子段再尝试与同阀门计划项「相邻相接」：
紧邻计划项之前的计为该分区的**提前运行**，紧邻之后的计为**延后运行**
（供提前 / 延后时间核算，水量仍属计划外，不并入已灌水量）；
夹在两个计划项之间或无法相接的子段标为无法归属。

分区核算
--------
- 已灌水量 = 归属子段的 Σ 实际流量 × 分钟（事件区间取并集，并发重叠分钟
  只计一次流量，不重复计量）；
- 缺水量 = max(0, 所需水量 − 已灌)；超灌 = max(0, 已灌 − 所需)；
- 实际运行窗口与计划窗口比较，给出提前开始 / 延后结束分钟数；
- 计划区间内未被归属事件覆盖的分钟数为中断分钟；
- 证据不足子段只登记，不计入任何分区的已灌 / 缺水。

实际运行冲突（逐分钟扫描事件区间）
---------------------------------
- 阀门并发：同一分钟同阀门开启的事件数 > 该阀门并发上限；
- 总流量：同一分钟全部开启事件的实际流量之和 > 总流量上限。

补灌建议（不改原计划）
----------------------
只对仍缺水且已编排、阀门有效的分区，在其剩余允许时段（允许窗口
− 原计划槽 − 停水槽）中按时间从早到晚贪心选取 15 分钟槽，逐槽检查：

- 加入本分区（按阀门额定流量）后总流量不超限（相对原计划占用）；
- 本阀门并发不超限（相对原计划占用）；
- 且在实际运行占用下同样不超限（建议不与已发生的实际运行打架）。

选不满时给出**最小缺口**（剩余缺水、剩余可用槽数）及无解原因
（停水占用 / 计划并发 / 实际并发 / 落在允许时段外）。
"""

from datetime import datetime, timedelta

SLOT_MIN = 15
SLOTS = 96
MIN_PER_DAY = 1440

# 子段归属类型
ATTRIBUTED = "attributed"      # 唯一归属某计划分区
OUT_OF_PLAN = "out_of_plan"    # 计划外运行
AMBIGUOUS = "ambiguous"        # 与多个计划项相交，证据不足

# 计划外子段与计划项的相邻关系
PREFIX = "prefix"     # 紧邻计划项之前：提前运行
SUFFIX = "suffix"     # 紧邻计划项之后：延后运行
GAP = "gap"           # 夹在两个计划项之间
DETACHED = "detached"  # 无法与任何计划项相接

# 分区结论
DEFICIT = "deficit"  # 缺水
SURPLUS = "surplus"  # 超灌
ENOUGH = "enough"    # 足量

EPS = 1e-6


# ---------------------------------------------------------------- 时间工具

def parse_day(raw):
    """解析 'YYYY-MM-DD'，拒绝带时间或时区的字符串。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("计划日期必须为字符串（YYYY-MM-DD）")
    text = raw.strip()
    if len(text) != 10:
        raise ValueError(f"计划日期格式错误：{raw}（应为 YYYY-MM-DD）")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"计划日期格式错误：{raw}（应为 YYYY-MM-DD）")


def parse_event_ts(raw, day, field):
    """解析事件时间戳为「距计划日期 00:00 的分钟数」。

    只接受本地时间（不带时区）、精确到分钟、且必须落在计划日期当天
    （00:00–次日 00:00，右端点 24:00 仅用于结束时间）。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"事件时间必须为字符串（YYYY-MM-DDTHH:MM）：{field}")
    text = raw.strip()
    if len(text) <= 10:
        raise ValueError(f"事件时间必须包含时分（YYYY-MM-DDTHH:MM）：{field}")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    tail = text[10:]
    if text.upper().endswith(("Z", "UTC")) or "+" in tail or "-" in tail:
        raise ValueError(f"事件时间不支持时区后缀，请使用本地时间：{field}")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"事件时间格式错误：{raw}（应为 YYYY-MM-DDTHH:MM）")
    if dt.tzinfo is not None:
        raise ValueError(f"事件时间不支持时区，请使用本地时间：{field}")
    if dt.second or dt.microsecond:
        raise ValueError(f"事件时间必须精确到分钟（秒为 0）：{field}")
    day_start = datetime.combine(day, datetime.min.time())
    minute = int((dt - day_start).total_seconds() // 60)
    if not 0 <= minute <= MIN_PER_DAY:
        raise ValueError(f"事件时间必须在计划日期 {day.isoformat()} 当天：{field}")
    return minute


def fmt_minute(day, minute):
    """分钟偏移 -> 'YYYY-MM-DDTHH:MM'（次日 00:00 显示为当日 24:00）。"""
    if minute == MIN_PER_DAY:
        return f"{day.isoformat()}T24:00"
    dt = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
    return dt.isoformat(timespec="minutes")


def fmt_hhmm(minute):
    """分钟偏移 -> 'HH:MM'（1440 -> '24:00'）。"""
    minute = int(minute)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def r2(x):
    """两位小数规整，去掉浮点噪声。"""
    v = round(float(x) + 0.0, 2)
    return 0.0 if abs(v) < 0.005 else v


# ---------------------------------------------------------------- 事件校验

def _as_float(raw, field):
    if isinstance(raw, bool):
        raise ValueError(f"{field} 必须为数字")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须为数字")
    if val != val or val < 0:
        raise ValueError(f"{field} 不能为负")
    return val


def validate_events(raw_events, day, valve_ids):
    """校验一批新提交的事件（不含库内已有事件）。

    返回按 (开始时间, event_id) 排序的规范化事件列表。
    批内事件开始时间必须非递减（允许同一开始时刻的并发事件）；
    event_id 批内唯一。
    """
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("events 必须为非空数组")
    cleaned = []
    seen_ids = set()
    for i, e in enumerate(raw_events):
        where = f"第 {i + 1} 条事件"
        if not isinstance(e, dict):
            raise ValueError(f"{where}：必须为对象")
        eid = e.get("event_id")
        if eid is None or not str(eid).strip():
            raise ValueError(f"{where}：缺少 event_id")
        eid = str(eid).strip()
        if eid in seen_ids:
            raise ValueError(f"{where}：批内 event_id 重复（{eid}）")
        seen_ids.add(eid)
        try:
            valve_id = int(e.get("valve_id"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}（{eid}）：阀门编号非法")
        if valve_id not in valve_ids:
            raise ValueError(f"{where}（{eid}）：阀门不存在（id={valve_id}）")
        start_min = parse_event_ts(e.get("start"), day, f"{where}（{eid}）开始时间")
        end_min = parse_event_ts(e.get("end"), day, f"{where}（{eid}）结束时间")
        if end_min <= start_min:
            raise ValueError(f"{where}（{eid}）：结束时间必须晚于开始时间")
        flow = _as_float(e.get("flow_lmin"), f"{where}（{eid}）实际流量")
        if i > 0 and start_min < cleaned[-1]["start_min"]:
            raise ValueError(
                f"{where}（{eid}）：批内事件必须按开始时间递增排列"
                f"（早于 {cleaned[-1]['event_id']}）")
        cleaned.append({
            "event_id": eid, "valve_id": valve_id,
            "start_min": start_min, "end_min": end_min,
            "flow_lmin": flow,
        })
    return sorted(cleaned, key=lambda e: (e["start_min"], e["event_id"]))


def merge_with_existing(new_events, existing):
    """新事件与库内事件合并，做幂等 / 冲突检查。

    existing: 库内同批次事件（同形状：event_id/valve_id/start_min/
              end_min/flow_lmin）。
    - 同 event_id 且四要素完全一致 -> 幂等跳过；
    - 同 event_id 但内容不一致     -> ValueError（调用方整批拒绝）。
    返回 (合并后事件, 新插入条数, 幂等跳过条数)。
    """
    by_id = {e["event_id"]: e for e in existing}
    inserted = 0
    skipped = 0
    for e in new_events:
        old = by_id.get(e["event_id"])
        sig = (e["valve_id"], e["start_min"], e["end_min"], r2(e["flow_lmin"]))
        if old is not None:
            old_sig = (old["valve_id"], old["start_min"], old["end_min"],
                       r2(old["flow_lmin"]))
            if sig != old_sig:
                raise ValueError(
                    f"event_id 冲突：{e['event_id']} 已存在且内容不同"
                    f"（同编号同内容幂等；内容不同请整批复核或先删除旧事件）")
            skipped += 1
            continue
        by_id[e["event_id"]] = e
        inserted += 1
    merged = sorted(by_id.values(), key=lambda e: (e["start_min"], e["event_id"]))
    return merged, inserted, skipped


# ---------------------------------------------------------------- 对齐切分

def _plan_intervals(items, zones_by_id):
    """计划项 -> [(lo, hi, zone_id)]，只保留分区仍存在的项。"""
    out = []
    for it in items:
        z = zones_by_id.get(it["zone_id"])
        if z is None:
            continue
        lo = it["start_slot"] * SLOT_MIN
        hi = lo + it["length"] * SLOT_MIN
        out.append((lo, hi, z["id"]))
    return out


def _union_minutes(intervals):
    """若干 [(lo,hi)] 的分钟并集长度。"""
    points = sorted(x for iv in intervals for x in ((iv[0], 1), (iv[1], -1)))
    total = 0
    depth = 0
    prev = None
    for t, d in points:
        if depth > 0 and prev is not None and t > prev:
            total += t - prev
        depth += d
        prev = t
    return total


def _segments_for_event(event, plans):
    """把一个事件按同阀门计划项端点切分并归属。

    plans: 该阀门的 [(lo, hi, zone_id)]。
    返回子段列表 [{lo, hi, kind, zone_id, candidates, adjacency, adj_zone_id}]。
    """
    cuts = {event["start_min"], event["end_min"]}
    for lo, hi, _z in plans:
        if hi > event["start_min"] and lo < event["end_min"]:
            cuts.add(max(lo, event["start_min"]))
            cuts.add(min(hi, event["end_min"]))
    cuts = sorted(cuts)

    segs = []
    for lo, hi in zip(cuts, cuts[1:]):
        hits = [(plo, phi, zid) for plo, phi, zid in plans
                if phi > lo and plo < hi]
        seg = {"lo": lo, "hi": hi}
        if len(hits) == 1:
            seg.update(kind=ATTRIBUTED, zone_id=hits[0][2],
                       candidates=[hits[0][2]], adjacency=None,
                       adj_zone_id=None)
        elif len(hits) >= 2:
            seg.update(kind=AMBIGUOUS, zone_id=None,
                       candidates=sorted({z for _, _, z in hits}),
                       adjacency=None, adj_zone_id=None)
        else:
            # 计划外：仅当端点恰好相接时归为提前 / 延后
            adj, adj_zone = DETACHED, None
            prefix = next((t for t in plans if t[0] == hi), None)
            suffix = next((t for t in plans if t[1] == lo), None)
            if prefix and suffix:
                adj = GAP
            elif prefix:
                adj, adj_zone = PREFIX, prefix[2]
            elif suffix:
                adj, adj_zone = SUFFIX, suffix[2]
            seg.update(kind=OUT_OF_PLAN, zone_id=None, candidates=[],
                       adjacency=adj, adj_zone_id=adj_zone)
        segs.append(seg)
    return segs


# ---------------------------------------------------------------- 冲突扫描

def _collect_conflict_spans(events, valves_by_id, valve_concur, total_flow):
    """以事件端点逐段切段，检出总流量 / 阀门并发冲突，再合并相邻区间。"""
    endpoints = sorted({t for e in events
                        for t in (e["start_min"], e["end_min"])})
    conflicts = []
    for lo, hi in zip(endpoints, endpoints[1:]):
        active = [e for e in events if e["start_min"] <= lo < hi <= e["end_min"]]
        if not active:
            continue
        flow = sum(e["flow_lmin"] for e in active)
        if total_flow is not None and flow > total_flow + EPS:
            conflicts.append({
                "type": "total_flow",
                "valve_id": None, "valve_name": None,
                "start_min": lo, "end_min": hi,
                "flow_lmin": r2(flow), "limit": r2(total_flow),
                "event_ids": sorted(e["event_id"] for e in active),
            })
        by_valve = {}
        for e in active:
            by_valve.setdefault(e["valve_id"], []).append(e)
        for vid, evs in by_valve.items():
            if len(evs) > valve_concur.get(vid, 1):
                conflicts.append({
                    "type": "valve_concur",
                    "valve_id": vid,
                    "valve_name": (valves_by_id.get(vid) or {}).get(
                        "name", f"阀门#{vid}"),
                    "start_min": lo, "end_min": hi,
                    "count": len(evs), "limit": valve_concur.get(vid, 1),
                    "event_ids": sorted(e["event_id"] for e in evs),
                })
    return _merge_conflicts(conflicts)


def _merge_conflicts(spans):
    """相邻且类型 / 对象 / 事件集合相同的冲突区间合并。"""
    merged = []
    for s in sorted(spans, key=lambda x: (x["type"], x["valve_id"] or 0,
                                          x["start_min"])):
        if merged:
            m = merged[-1]
            if (m["type"] == s["type"] and m["valve_id"] == s["valve_id"]
                    and m["event_ids"] == s["event_ids"]
                    and m["end_min"] == s["start_min"]):
                m["end_min"] = s["end_min"]
                if s["type"] == "total_flow":
                    m["flow_lmin"] = r2(max(m["flow_lmin"], s["flow_lmin"]))
                else:
                    m["count"] = max(m["count"], s["count"])
                continue
        merged.append(dict(s))
    return sorted(merged, key=lambda x: (x["start_min"], x["type"]))


# ---------------------------------------------------------------- 占用数组

def _plan_occupancy(items, zones_by_id):
    """原计划逐分钟占用：flow_sum[1440]、valve_count{vid:[1440]}。"""
    flow_sum = [0.0] * MIN_PER_DAY
    valve_count = {}
    for it in items:
        z = zones_by_id.get(it["zone_id"])
        if z is None:
            continue
        lo = it["start_slot"] * SLOT_MIN
        hi = min(lo + it["length"] * SLOT_MIN, MIN_PER_DAY)
        arr = valve_count.setdefault(z["valve_id"], [0] * MIN_PER_DAY)
        for m in range(lo, hi):
            flow_sum[m] += z.get("flow_lmin", 0.0)
            arr[m] += 1
    return flow_sum, valve_count


def _actual_occupancy(events):
    """实际运行逐分钟占用（总流量求和、同阀门并发计数）。"""
    flow_sum = [0.0] * MIN_PER_DAY
    valve_count = {}
    for e in events:
        arr = valve_count.setdefault(e["valve_id"], [0] * MIN_PER_DAY)
        for m in range(e["start_min"], min(e["end_min"], MIN_PER_DAY)):
            flow_sum[m] += e["flow_lmin"]
            arr[m] += 1
    return flow_sum, valve_count


# ---------------------------------------------------------------- 补灌建议

def _slot_blocked_reasons(slot, zone, valve, plan_flow, plan_valve,
                          actual_flow, actual_valve, total_flow):
    """返回该候选槽不可用的原因列表；空列表表示可用（逐分钟检查整槽）。"""
    flow = valve["flow_lmin"]
    vid = zone["valve_id"]
    limit = total_flow if total_flow is not None else float("inf")
    reasons = []
    pv = plan_valve.get(vid)
    av = actual_valve.get(vid)
    for m in range(slot * SLOT_MIN, (slot + 1) * SLOT_MIN):
        if plan_flow[m] + flow > limit + EPS:
            reasons.append("总流量超限（叠加原计划占用）")
            break
    if pv is not None:
        for m in range(slot * SLOT_MIN, (slot + 1) * SLOT_MIN):
            if pv[m] + 1 > valve["concur"]:
                reasons.append(f"阀门 {valve['name']} 并发超限（叠加原计划占用）")
                break
    for m in range(slot * SLOT_MIN, (slot + 1) * SLOT_MIN):
        if actual_flow[m] + flow > limit + EPS:
            reasons.append("总流量超限（叠加实际运行占用）")
            break
    if av is not None:
        for m in range(slot * SLOT_MIN, (slot + 1) * SLOT_MIN):
            if av[m] + 1 > valve["concur"]:
                reasons.append(f"阀门 {valve['name']} 并发超限（叠加实际运行占用）")
                break
    return list(dict.fromkeys(reasons))


def _merge_slots(slots):
    """连续槽位 -> [[start_slot, end_slot, n]]。"""
    if not slots:
        return []
    slots = sorted(slots)
    blocks = []
    start = prev = slots[0]
    for s in slots[1:]:
        if s == prev + 1:
            prev = s
            continue
        blocks.append([start, prev + 1, prev - start + 1])
        start = prev = s
    blocks.append([start, prev + 1, prev - start + 1])
    return blocks


def _block_dicts(blocks):
    return [{
        "start_slot": b[0], "end_slot": b[1],
        "start": fmt_hhmm(b[0] * SLOT_MIN),
        "end": fmt_hhmm(b[1] * SLOT_MIN),
        "duration_min": b[2] * SLOT_MIN,
    } for b in blocks]


def makeup_for_zone(zone, valve, items, blackout_slots, plan_flow, plan_valve,
                    actual_flow, actual_valve, total_flow, deficit_l):
    """为单个缺水分区生成不改原计划的补灌建议。"""
    ew, lw = zone["earliest_slot"], zone["latest_slot"]
    planned_slots = set()
    for it in items:
        if it["zone_id"] == zone["id"]:
            planned_slots.update(range(it["start_slot"],
                                       it["start_slot"] + it["length"]))
    slot_water = valve["flow_lmin"] * SLOT_MIN
    need_slots = int(-(-deficit_l // slot_water))  # ceil

    free_slots = []
    reason_slots = {}
    for s in range(ew, lw):
        if s in planned_slots:
            continue  # 原计划槽不属于「剩余允许时段」
        if s in blackout_slots:
            reason_slots.setdefault("停水时段", []).append(s)
            continue
        reasons = _slot_blocked_reasons(
            s, zone, valve, plan_flow, plan_valve, actual_flow, actual_valve,
            total_flow)
        if reasons:
            reason_slots.setdefault("；".join(reasons), []).append(s)
        else:
            free_slots.append(s)

    chosen = free_slots[:need_slots]
    blocks = _merge_slots(chosen)
    remaining = max(0.0, deficit_l - len(chosen) * slot_water)
    feasible = len(chosen) >= need_slots
    if not chosen:
        reason = "剩余允许时段内没有同时满足停水 / 总流量 / 并发约束的槽位"
    elif not feasible:
        reason = (f"剩余允许时段仅 {len(free_slots)} 个可用槽"
                  f"（{len(free_slots) * SLOT_MIN} 分钟），"
                  f"不足所需 {need_slots} 槽")
    else:
        reason = None
    return {
        "zone_id": zone["id"], "zone_name": zone["name"],
        "valve_id": valve["id"], "valve_name": valve["name"],
        "flow_lmin": valve["flow_lmin"],
        "deficit_l": r2(deficit_l),
        "need_slots": need_slots,
        "chosen_slots": chosen,
        "blocks": _block_dicts(blocks),
        "delivered_l": r2(min(len(chosen) * slot_water,
                              need_slots * slot_water)),
        "feasible": feasible,
        "remaining_l": r2(remaining),
        "free_slot_count": len(free_slots),
        "free_windows": _block_dicts(_merge_slots(free_slots)),
        "blocked": [{"reason": k, "slot_count": len(v)}
                    for k, v in sorted(reason_slots.items())],
        "reason": reason,
    }


# ---------------------------------------------------------------- 全日核验

def _union_water(attributed):
    """归属区间 [(lo,hi,event_id,flow)] 的并集水量。

    重叠分钟取并集事件流量最大值，避免并发 / 重复事件重复计量。
    """
    if not attributed:
        return 0.0
    endpoints = sorted({t for lo, hi, *_ in attributed for t in (lo, hi)})
    total = 0.0
    for lo, hi in zip(endpoints, endpoints[1:]):
        flows = [f for a, b, _e, f in attributed if a <= lo and b >= hi]
        if flows:
            total += max(flows) * (hi - lo)
    return r2(total)


def check_day(zones, valves, items, blackouts, events, day, total_flow,
              slot_min=SLOT_MIN):
    """按计划日期对一批实际运行事件做全量执行核验（纯计算，不改原计划）。

    zones:    [{id, name, water_l, valve_id, flow_lmin,
                earliest_slot, latest_slot}]
    valves:   [{id, name, flow_lmin, concur}]
    items:    [{zone_id, start_slot, length}]（当前原计划，槽对齐）
    blackouts:[{start_slot, end_slot}]
    events:   规范化事件（含 start_min/end_min 分钟偏移）
    day:      datetime.date
    """
    zones_by_id = {z["id"]: z for z in zones}
    valves_by_id = {v["id"]: v for v in valves}
    plans_all = _plan_intervals(items, zones_by_id)
    plans_by_valve = {}
    for lo, hi, zid in plans_all:
        plans_by_valve.setdefault(zones_by_id[zid]["valve_id"], []).append(
            (lo, hi, zid))

    bo_slots = set()
    for b in blackouts:
        bo_slots.update(range(max(0, b["start_slot"]),
                              min(SLOTS, b["end_slot"])))

    # ---- 切分 / 归属 ----
    zone_attr_iv = {z["id"]: [] for z in zones}
    zone_link_iv = {z["id"]: [] for z in zones}
    zone_segs = {z["id"]: [] for z in zones}
    ambiguous_segs = []
    out_segs = []
    event_details = []

    for e in events:
        segs = _segments_for_event(e, plans_by_valve.get(e["valve_id"], []))
        detail = {
            "event_id": e["event_id"], "valve_id": e["valve_id"],
            "valve_name": (valves_by_id.get(e["valve_id"]) or {}).get("name"),
            "start_min": e["start_min"], "end_min": e["end_min"],
            "start": fmt_minute(day, e["start_min"]),
            "end": fmt_minute(day, e["end_min"]),
            "flow_lmin": r2(e["flow_lmin"]),
            "duration_min": e["end_min"] - e["start_min"],
            "segments": [],
        }
        for sg in segs:
            minutes = sg["hi"] - sg["lo"]
            row = {
                "event_id": e["event_id"],
                "start_min": sg["lo"], "end_min": sg["hi"],
                "start": fmt_hhmm(sg["lo"]),
                "end": fmt_hhmm(sg["hi"]),
                "minutes": minutes,
                "water_l": r2(e["flow_lmin"] * minutes),
                "kind": sg["kind"], "zone_id": sg["zone_id"],
                "zone_name": (zones_by_id.get(sg["zone_id"]) or {}).get("name")
                if sg["zone_id"] else None,
                "candidate_zones": [
                    {"zone_id": zid, "zone_name": zones_by_id[zid]["name"]}
                    for zid in sg["candidates"]],
                "adjacency": sg["adjacency"],
                "adj_zone_id": sg["adj_zone_id"],
            }
            detail["segments"].append(row)
            if sg["kind"] == ATTRIBUTED:
                zone_attr_iv[sg["zone_id"]].append(
                    (sg["lo"], sg["hi"], e["event_id"], e["flow_lmin"]))
                zone_segs[sg["zone_id"]].append(row)
            elif sg["kind"] == AMBIGUOUS:
                ambiguous_segs.append({
                    **row, "valve_id": e["valve_id"],
                    "valve_name": detail["valve_name"],
                })
            else:
                if sg["adj_zone_id"] is not None:
                    zone_link_iv[sg["adj_zone_id"]].append(
                        (sg["lo"], sg["hi"], e["event_id"], e["flow_lmin"],
                         sg["adjacency"]))
                    zone_segs[sg["adj_zone_id"]].append(row)
                out_segs.append({
                    **row, "valve_id": e["valve_id"],
                    "valve_name": detail["valve_name"],
                })
        event_details.append(detail)

    # ---- 分区核算 ----
    plan_flow, plan_valve = _plan_occupancy(items, zones_by_id)
    actual_flow, actual_valve = _actual_occupancy(events)

    zone_rows = []
    deficit_zones = []
    for z in zones:
        zid = z["id"]
        valve = valves_by_id.get(z["valve_id"])
        zitems = [it for it in items if it["zone_id"] == zid]
        plan_lo = min((it["start_slot"] * SLOT_MIN for it in zitems),
                      default=None)
        plan_hi = max(((it["start_slot"] + it["length"]) * SLOT_MIN
                       for it in zitems), default=None)
        attr = zone_attr_iv[zid]
        links = zone_link_iv[zid]

        delivered = _union_water(attr)
        target = float(z["water_l"])
        deficit = max(0.0, target - delivered)
        surplus = max(0.0, delivered - target)
        status = DEFICIT if deficit > EPS else \
            SURPLUS if surplus > EPS else ENOUGH

        prefix_iv = [(lo, hi) for lo, hi, _e, _f, kind in links
                     if kind == PREFIX]
        suffix_iv = [(lo, hi) for lo, hi, _e, _f, kind in links
                     if kind == SUFFIX]
        timing_iv = ([(lo, hi) for lo, hi, *_ in attr]
                     + prefix_iv + suffix_iv)
        early_min = late_min = 0
        actual_lo = actual_hi = None
        if timing_iv:
            actual_lo = min(iv[0] for iv in timing_iv)
            actual_hi = max(iv[1] for iv in timing_iv)
        if plan_lo is not None:
            early_min = max(0, plan_lo - actual_lo) if actual_lo is not None else 0
            late_min = max(0, actual_hi - plan_hi) if actual_hi is not None else 0

        covered = _union_minutes([(lo, hi) for lo, hi, *_ in attr])
        plan_minutes = sum(it["length"] * SLOT_MIN for it in zitems)
        interrupt = max(0, plan_minutes - covered) if plan_minutes else 0
        out_l = sum(f * (hi - lo) for lo, hi, _e, f, _k in links)

        zone_rows.append({
            "zone_id": zid, "zone_name": z["name"],
            "valve_id": z["valve_id"],
            "valve_name": valve["name"] if valve else "（阀门已删除）",
            "scheduled": bool(zitems),
            "plan_start": fmt_hhmm(plan_lo) if plan_lo is not None else None,
            "plan_end": fmt_hhmm(plan_hi) if plan_hi is not None else None,
            "actual_start": fmt_hhmm(actual_lo) if actual_lo is not None else None,
            "actual_end": (fmt_hhmm(actual_hi) if actual_hi is not None
                           and actual_hi < MIN_PER_DAY
                           else ("24:00" if actual_hi == MIN_PER_DAY else None)),
            "target_l": r2(target),
            "delivered_l": r2(delivered),
            "deficit_l": r2(deficit),
            "surplus_l": r2(surplus),
            "early_min": early_min,
            "late_min": late_min,
            "interrupt_min": interrupt,
            "out_of_plan_l": r2(out_l),
            "status": status,
            "segments": sorted(zone_segs[zid],
                               key=lambda s: (s["start_min"], s["event_id"])),
        })
        if status == DEFICIT and valve is not None and zitems:
            deficit_zones.append((z, valve, deficit))

    # ---- 补灌建议（紧迫度排序与 scheduler.generate 一致）----
    suggestions = []
    for z, valve, deficit in sorted(
            deficit_zones,
            key=lambda zv: ((zv[0]["latest_slot"] - zv[0]["earliest_slot"]),
                            -zv[0]["water_l"], zv[0]["id"])):
        suggestions.append(makeup_for_zone(
            z, valve, items, bo_slots, plan_flow, plan_valve,
            actual_flow, actual_valve, total_flow, deficit))

    # ---- 冲突 ----
    conflicts = _collect_conflict_spans(
        events, valves_by_id,
        {v["id"]: v["concur"] for v in valves}, total_flow)
    for c in conflicts:
        c["start"] = fmt_hhmm(c["start_min"])
        c["end"] = fmt_hhmm(c["end_min"])
        c["minutes"] = c["end_min"] - c["start_min"]

    summary = {
        "date": day.isoformat(),
        "event_count": len(events),
        "zone_count": len(zones),
        "scheduled_count": sum(1 for r in zone_rows if r["scheduled"]),
        "delivered_l": r2(sum(r["delivered_l"] for r in zone_rows)),
        "target_l": r2(sum(r["target_l"] for r in zone_rows)),
        "deficit_l": r2(sum(r["deficit_l"] for r in zone_rows)),
        "surplus_l": r2(sum(r["surplus_l"] for r in zone_rows)),
        "out_of_plan_l": r2(sum(s["water_l"] for s in out_segs)),
        "ambiguous_l": r2(sum(s["water_l"] for s in ambiguous_segs)),
        "deficit_zones": [r["zone_id"] for r in zone_rows
                          if r["status"] == DEFICIT],
        "surplus_zones": [r["zone_id"] for r in zone_rows
                          if r["status"] == SURPLUS],
        "conflict_count": len(conflicts),
        "ambiguous_count": len(ambiguous_segs),
        "out_of_plan_count": len(out_segs),
        "makeup_feasible": all(s["feasible"] for s in suggestions),
        "makeup_unsolved_l": r2(sum(s["remaining_l"] for s in suggestions)),
    }

    return {
        "date": day.isoformat(),
        "slot_minutes": slot_min,
        "status_labels": {
            DEFICIT: "缺水", SURPLUS: "超灌", ENOUGH: "足量",
            ATTRIBUTED: "计划内归属", OUT_OF_PLAN: "计划外运行",
            AMBIGUOUS: "证据不足",
        },
        "summary": summary,
        "zones": zone_rows,
        "events": event_details,
        "ambiguous_segments": ambiguous_segs,
        "out_of_plan_segments": out_segs,
        "conflicts": conflicts,
        "makeup": suggestions,
    }
