"""用水回溯核心：把 15 分钟灌溉时段与水源「半开状态区间」求交。

水质页在「连续两次」状态机（见 water_quality.py）之上，按**计划日期**回答：
水质异常当天，哪些轮灌分区用了风险水、用了多少。

区间构造（按水源、按指标、按采样时间顺序）
------------------------------------------
状态在采样时刻生效并**持续到下一条采样**，即每条采样 s_i 对应一个
**左闭右开**区间 [ts_i, ts_{i+1})；最后一条采样的状态向后无限延续。

- 当日首条采样**之前**的时段：沿用当日之前最近一条历史采样的状态
  （「采样时刻起采用新状态」，历史状态只在采样时刻前有效）；
- 若该水源在当日之前没有任何历史采样（当日第一条就是全水源第一条），
  当日首条采样之前没有任何状态证据，标为「证据不足」（INSUFFICIENT）。

用水区间归类
------------
对每个分区当日的 15 分钟灌溉时段（[开始槽, 结束槽)）与上述区间求交：

- 交集落在「超标」区间 -> RISK（风险水量）；
- 交集落在「观察」区间 -> REVIEW（待复核水量）；
- 交集落在「正常」区间 -> SAFE；
- 交集落在证据不足区间 / 阀门未绑定水源 -> INSUFFICIENT（证据不足，待复核）。

水源整体状态取四项指标最差者（与 water_quality.recompute_source 一致），
同时保留每个非「正常」指标作为触发指标。

水量 = 阀门额定流量 (L/min) × 重叠分钟数。采样时刻可能落在 15 分钟槽中间，
因此重叠分钟允许为小数。

本模块只做纯计算，不读写数据库；区间构建复用 wq.recompute_source。
"""

from datetime import datetime, timedelta

import water_quality as wq

RISK = "risk"                  # 超标区间用水
REVIEW = "review"              # 观察区间用水
SAFE = "safe"                  # 正常区间用水
INSUFFICIENT = "insufficient"  # 无历史状态可沿用 / 阀门未绑定水源

CLASS_LABEL = {
    RISK: "风险", REVIEW: "待复核", SAFE: "正常", INSUFFICIENT: "证据不足",
}
CLASS_RANK = {RISK: 0, REVIEW: 1, INSUFFICIENT: 2, SAFE: 3}

# 水源整体状态 -> 用水归类
_STATUS_CLASS = {
    wq.EXCEEDED: RISK,
    wq.OBSERVING: REVIEW,
    wq.NORMAL: SAFE,
}

SAMPLE = "sample"          # 状态来自当日采样
CARRY = "carry"            # 沿用当日之前最近历史采样
NO_HISTORY = "insufficient"  # 无任何历史样本
UNBOUND = "unbound"        # 阀门未绑定水源


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


def _dt(ts):
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------- 状态区间

def status_segments(samples, day, carry_sample):
    """构造覆盖当日 [day 00:00, day+1 00:00) 的左闭右开状态区间序列。

    samples:      当日采样（已过 wq.recompute_source，含整体状态），按 ts 升序；
    carry_sample: 当日之前最近一条历史采样（同样含状态），无则 None。

    返回 [{start, end, status, triggers, metric_states, source}]：
      - start/end 为本地 ISO 字符串（秒），区间左闭右开，覆盖整个当日；
      - source = (kind, sample)：
          (SAMPLE, s)       采样时刻起采用该采样的新状态；
          (CARRY, s)        沿用最近历史采样（当日首条采样之前）；
          (NO_HISTORY, None) 无任何历史样本，证据不足。
    """
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    segments = []

    def add(start, end, status, triggers, mstates, source):
        if end <= start:
            return
        segments.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "status": status, "triggers": triggers,
            "metric_states": mstates, "source": source,
        })

    if samples:
        first_dt = _dt(samples[0]["ts"])
        if first_dt > day_start:
            if carry_sample is not None:
                add(day_start, first_dt,
                    carry_sample["status"], list(carry_sample["triggers"]),
                    carry_sample["metric_states"], (CARRY, carry_sample))
            else:
                add(day_start, first_dt,
                    INSUFFICIENT, [], None, (NO_HISTORY, None))
    elif carry_sample is not None:
        # 当日无采样：全天沿用最近历史状态
        add(day_start, day_end,
            carry_sample["status"], list(carry_sample["triggers"]),
            carry_sample["metric_states"], (CARRY, carry_sample))
    else:
        add(day_start, day_end, INSUFFICIENT, [], None, (NO_HISTORY, None))

    # 每条当日采样产生 [ts_i, ts_{i+1})，末条开放到当日结束
    for i, s in enumerate(samples):
        start = max(_dt(s["ts"]), day_start)
        if start >= day_end:
            break
        end = _dt(samples[i + 1]["ts"]) if i + 1 < len(samples) else day_end
        end = min(end, day_end)
        add(start, end, s["status"], list(s["triggers"]),
            s["metric_states"], (SAMPLE, s))

    segments.sort(key=lambda g: g["start"])
    return segments


# ---------------------------------------------------------------- 求交

def _intersect_minutes(seg_start, seg_end, win_start, win_end):
    lo = max(seg_start, win_start)
    hi = min(seg_end, win_end)
    delta = (hi - lo).total_seconds() / 60.0
    return delta if delta > 0 else 0.0


def _window(day, start_slot, length, slot_min):
    day_start = datetime.combine(day, datetime.min.time())
    win_start = day_start + timedelta(minutes=start_slot * slot_min)
    return win_start, win_start + timedelta(minutes=length * slot_min)


def _unclassified_row(zone, valve, source_id, source_name, day, slot_min, kind):
    """阀门缺失 / 未绑定水源：灌溉时段整体登记为证据不足（不出具水量结论）。"""
    win_start, win_end = _window(day, zone["start_slot"], zone["length"], slot_min)
    minutes = (win_end - win_start).total_seconds() / 60.0
    return {
        "zone_id": zone["id"],
        "zone_name": zone["name"],
        "valve_id": zone.get("valve_id"),
        "valve_name": valve["name"] if valve else None,
        "source_id": source_id,
        "source_name": source_name,
        "flow_lmin": float(valve["flow_lmin"]) if valve else 0.0,
        "start_slot": zone["start_slot"],
        "length": zone["length"],
        "start": win_start.isoformat(timespec="minutes"),
        "end": win_end.isoformat(timespec="minutes"),
        "planned_minutes": zone["length"] * slot_min,
        "classified_minutes": 0.0,
        "klass": INSUFFICIENT,
        "risk_l": 0.0, "review_l": 0.0, "safe_l": 0.0, "insufficient_l": 0.0,
        "triggers": [],
        "overlaps": [{
            "start": win_start.isoformat(timespec="minutes"),
            "end": win_end.isoformat(timespec="minutes"),
            "minutes": round(minutes, 2),
            "water_l": 0.0,
            "status": INSUFFICIENT,
            "klass": INSUFFICIENT,
            "triggers": [],
            "metric_states": None,
            "source_kind": kind,
            "source_sample_id": None,
            "source_sample_ts": None,
        }],
    }


def trace_zone(zone, valve, source_id, source_name, segments, day, slot_min=15):
    """对单个已编排分区计算当日用水回溯明细。

    zone:     {id, name, valve_id, start_slot, length}
    valve:    {id, name, flow_lmin} 或 None（分区阀门已缺失）
    segments: 所绑水源的当日状态区间（status_segments 输出）；
              阀门未绑定 / 缺失时传 None。
    """
    if valve is None or source_id is None:
        kind = UNBOUND if valve else NO_HISTORY
        return _unclassified_row(
            zone, valve, source_id if valve else None, source_name,
            day, slot_min, kind)

    win_start, win_end = _window(
        day, zone["start_slot"], zone["length"], slot_min)
    flow = float(valve["flow_lmin"])

    overlaps = []
    totals = {RISK: 0.0, REVIEW: 0.0, SAFE: 0.0, INSUFFICIENT: 0.0}
    trigger_set = []

    for seg in segments:
        minutes = _intersect_minutes(
            _dt(seg["start"]), _dt(seg["end"]), win_start, win_end)
        if minutes <= 0:
            continue
        kind, src_sample = seg["source"]
        klass = _STATUS_CLASS.get(seg["status"], INSUFFICIENT)
        liters = flow * minutes
        totals[klass] += liters
        for t in seg["triggers"]:
            if t not in trigger_set:
                trigger_set.append(t)
        overlaps.append({
            "start": max(_dt(seg["start"]), win_start)
                     .isoformat(timespec="minutes"),
            "end": min(_dt(seg["end"]), win_end)
                   .isoformat(timespec="minutes"),
            "minutes": round(minutes, 2),
            "water_l": round(liters, 2),
            "status": seg["status"],
            "klass": klass,
            "triggers": list(seg["triggers"]),
            "metric_states": seg["metric_states"],
            "source_kind": kind,
            "source_sample_id": src_sample["id"] if src_sample else None,
            "source_sample_ts": src_sample["ts"] if src_sample else None,
        })

    overlaps.sort(key=lambda o: (o["start"], o["end"]))
    classified_minutes = sum(o["minutes"] for o in overlaps)
    zone_class = RISK if totals[RISK] > 0 else \
        REVIEW if totals[REVIEW] > 0 else \
        INSUFFICIENT if totals[INSUFFICIENT] > 0 else SAFE

    return {
        "zone_id": zone["id"],
        "zone_name": zone["name"],
        "valve_id": zone.get("valve_id"),
        "valve_name": valve["name"],
        "source_id": source_id,
        "source_name": source_name,
        "flow_lmin": flow,
        "start_slot": zone["start_slot"],
        "length": zone["length"],
        "start": win_start.isoformat(timespec="minutes"),
        "end": win_end.isoformat(timespec="minutes"),
        "planned_minutes": zone["length"] * slot_min,
        "classified_minutes": round(classified_minutes, 2),
        "klass": zone_class,
        "risk_l": round(totals[RISK], 2),
        "review_l": round(totals[REVIEW], 2),
        "safe_l": round(totals[SAFE], 2),
        "insufficient_l": round(totals[INSUFFICIENT], 2),
        "triggers": trigger_set,
        "overlaps": overlaps,
    }


# ---------------------------------------------------------------- 全日回溯

def trace_day(zones, items, valves, samples_by_source, ranges_by_source,
              day, slot_min=15, source_names=None, zone_filter=None):
    """按计划日期 day 对已编排分区做用水回溯（纯计算，不改动计划）。

    zones:   [{id, name, valve_id}]
    items:   [{zone_id, start_slot, length}]（当日 15 分钟计划）
    valves:  [{id, name, flow_lmin, source_id}]；source_id 为 None 表示未绑定
    samples_by_source: {source_id: [原始采样行, ...]}（含 id/source_id/ts 与
                        四项指标，顺序不限，内部统一排序重算）
    ranges_by_source:  {source_id: {metric: (low, high)}}
    source_names:      {source_id: name}，用于结果展示
    zone_filter:       仅回溯该 zone_id（None 表示全部）
    """
    source_names = source_names or {}
    valve_by_id = {v["id"]: v for v in valves}
    zone_by_id = {z["id"]: z for z in zones}
    day_start = datetime.combine(day, datetime.min.time())

    # 每个绑定水源先全量重算状态，再切出历史沿用样本与当日样本
    segments_by_source = {}
    for source_id, raw in samples_by_source.items():
        ordered = sorted(raw, key=lambda s: (s["ts"], s["id"]))
        recomputed, _summary = wq.recompute_source(
            [{k: s[k] for k in
              ("id", "source_id", "ts", "ph", "ec", "turbidity", "nitrate")}
             for s in ordered],
            ranges_by_source[source_id])
        today_samples = [s for s in recomputed if s["ts"][:10] == day.isoformat()]
        carry = None
        for s in recomputed:
            if _dt(s["ts"]) < day_start:
                carry = s
            else:
                break
        segments_by_source[source_id] = status_segments(today_samples, day, carry)

    rows = []
    unbound_valve_ids = set()
    for it in sorted(items, key=lambda i: (i["start_slot"], i["zone_id"])):
        zone = zone_by_id.get(it["zone_id"])
        if zone is None:
            continue
        if zone_filter is not None and zone["id"] != zone_filter:
            continue
        z = {
            "id": zone["id"], "name": zone["name"],
            "valve_id": zone.get("valve_id"),
            "start_slot": it["start_slot"], "length": it["length"],
        }
        valve = valve_by_id.get(z["valve_id"]) if z["valve_id"] is not None else None
        source_id = valve.get("source_id") if valve else None
        if valve and source_id is None:
            unbound_valve_ids.add(valve["id"])
        segments = segments_by_source.get(source_id) if source_id else None
        rows.append(trace_zone(
            z, valve, source_id,
            source_names.get(source_id) if source_id else None,
            segments, day, slot_min))

    rows.sort(key=lambda r: (
        CLASS_RANK[r["klass"]], r["start_slot"], r["zone_id"]))

    unbound_valves = [
        {"id": vid, "name": valve_by_id[vid]["name"]}
        for vid in sorted(unbound_valve_ids) if vid in valve_by_id]

    summary = {
        "date": day.isoformat(),
        "zone_count": len(rows),
        "risk_l": round(sum(r["risk_l"] for r in rows), 2),
        "review_l": round(sum(r["review_l"] for r in rows), 2),
        "safe_l": round(sum(r["safe_l"] for r in rows), 2),
        "insufficient_l": round(sum(r["insufficient_l"] for r in rows), 2),
        "risk_zones": [r["zone_id"] for r in rows if r["klass"] == RISK],
        "review_zones": [r["zone_id"] for r in rows if r["klass"] == REVIEW],
        "insufficient_zones": [r["zone_id"] for r in rows
                               if r["klass"] == INSUFFICIENT],
    }

    return {
        "date": day.isoformat(),
        "slot_minutes": slot_min,
        "class_labels": CLASS_LABEL,
        "summary": summary,
        "unbound_valves": unbound_valves,
        "zones": rows,
    }
