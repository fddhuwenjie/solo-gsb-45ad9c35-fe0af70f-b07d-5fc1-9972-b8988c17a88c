"""轮灌编排核心：15 分钟粒度、总流量 / 阀门并发 / 停水时段约束。

时间以「槽」表示：0 = 00:00，1 = 00:15 …… 95 = 23:45，全天 96 槽。
"""

import math

SLOTS = 96
SLOT_MIN = 15


def hhmm_to_slot(text):
    """'07:30' -> 30。支持 '24:00' 表示当天结束（96，仅作右端点）。"""
    text = str(text).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式错误：{text}（应为 HH:MM）")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"时间超出范围：{text}")
    if m % SLOT_MIN:
        raise ValueError(f"时间必须为 {SLOT_MIN} 分钟整数倍：{text}")
    return h * 4 + m // SLOT_MIN


def slot_to_hhmm(slot):
    slot = int(round(slot))
    slot = max(0, min(SLOTS, slot))
    return f"{slot // 4:02d}:{slot % 4 * 15:02d}"


def slots_for_water(water_l, flow_lmin):
    """按额定流量计算至少需要多少个 15 分钟槽（向上取整）。"""
    if flow_lmin is None or flow_lmin <= 0:
        raise ValueError("阀门额定流量必须大于 0")
    if water_l is None or water_l < 0:
        raise ValueError("所需水量不能为负")
    n = math.ceil(float(water_l) / (float(flow_lmin) * SLOT_MIN))
    return max(1, int(n))


def blackout_slots(blackouts):
    """返回落在停水区间内的槽位集合。"""
    result = set()
    for b in blackouts:
        start = b.get("start_slot")
        end = b.get("end_slot")
        if start is None or end is None or end <= start:
            continue
        result.update(range(max(0, start), min(SLOTS, end)))
    return result


def zone_run_slots(start_slot, length):
    return set(range(start_slot, start_slot + length))


def _zone_violations(zone, length, run, blackouts_set, total_flow, flow_used,
                     valve_used, valve_by_id):
    """计算某个分区摆放在 run 槽段上的冲突。

    flow_used / valve_used 已包含其他分区占用（不含本分区）。
    返回 {槽: [原因, ...]}。
    """
    violations = {}
    ew, lw = zone["earliest_slot"], zone["latest_slot"]
    vid = zone["valve_id"]
    valve = valve_by_id.get(vid)

    def add(s, reason):
        violations.setdefault(s, []).append(reason)

    for s in sorted(run):
        if s < ew or s >= lw:
            add(s, "超出允许灌溉时段")
        if s in blackouts_set:
            add(s, "停水时段")

    for s in sorted(run):
        if total_flow is not None and flow_used[s] + zone["flow_lmin"] > total_flow:
            used = flow_used[s] + zone["flow_lmin"]
            add(s, f"总流量超限（{fmt_num(used)} > {fmt_num(total_flow)} L/min）")
        if valve and valve_used[vid][s] + 1 > valve["concur"]:
            add(s, f"阀门 {valve['name']} 并发超限（>{valve['concur']}）")
    return violations


def fmt_num(x):
    """60.0 -> '60'，62.5 -> '62.5'。"""
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def _compress_issues(violations, slot_count):
    """{槽: [原因]} -> 排序去重的原因列表。"""
    order = {"超出允许灌溉时段": 0, "停水时段": 1}
    reasons = set()
    for lst in violations.values():
        reasons.update(lst)

    def key(r):
        for prefix, p in order.items():
            if r.startswith(prefix):
                return (p, r)
        return (2, r)

    return sorted(reasons, key=key)


def evaluate(zones, valves, blackouts, items, total_flow):
    """对一份完整计划做全量校验。

    items: [{zone_id, start_slot, length}]
    返回 {issues:[{zone_id,start_slot,end_slot,reasons}],
          unscheduled:[{zone_id, reasons}],
          flow_used:[96], max_flow, valid}
    """
    valve_by_id = {v["id"]: v for v in valves}
    bo = blackout_slots(blackouts)

    flow_used = [0] * SLOTS
    valve_used = {v["id"]: [0] * SLOTS for v in valves}
    zones_by_id = {z["id"]: z for z in zones}

    # 先累计占用，再逐分区校验，从而把同槽冲突同时报给各方
    for it in items:
        z = zones_by_id.get(it["zone_id"])
        if not z:
            continue
        for s in zone_run_slots(it["start_slot"], it["length"]):
            if 0 <= s < SLOTS:
                flow_used[s] += z["flow_lmin"]
                valve_used[z["valve_id"]][s] += 1

    issues = []
    for it in items:
        z = zones_by_id.get(it["zone_id"])
        if not z:
            continue
        run = zone_run_slots(it["start_slot"], it["length"])
        # 占用表包含全部分区，需扣除本分区自身后再评估
        for s in run:
            if 0 <= s < SLOTS:
                flow_used[s] -= z["flow_lmin"]
                valve_used[z["valve_id"]][s] -= 1
        violations = _zone_violations(
            z, it["length"], run, bo, total_flow, flow_used, valve_used, valve_by_id)
        for s in run:
            if 0 <= s < SLOTS:
                flow_used[s] += z["flow_lmin"]
                valve_used[z["valve_id"]][s] += 1
        if violations:
            slots = sorted(violations)
            issues.append({
                "zone_id": z["id"],
                "zone_name": z["name"],
                "start_slot": min(slots),
                "end_slot": max(slots) + 1,
                "reasons": _compress_issues(violations, it["length"]),
            })

    unscheduled = []
    for z in zones:
        if not any(it["zone_id"] == z["id"] for it in items):
            if not items:
                reasons = ["尚未安排，点击「自动编排」或拖动生成时段"]
            else:
                reasons = diagnose_unscheduled(
                    z, zones, valves, blackouts, items, total_flow)
            unscheduled.append({
                "zone_id": z["id"],
                "zone_name": z["name"],
                "reasons": reasons,
            })

    return {
        "issues": issues,
        "unscheduled": unscheduled,
        "flow_used": flow_used,
        "max_flow": max(flow_used) if flow_used else 0,
        "valid": not issues and not unscheduled,
    }


def diagnose_unscheduled(zone, zones, valves, blackouts, items, total_flow):
    """为未排上的分区给出尽可能具体的冲突原因（在当前占用下选最好的位置评估）。"""
    valve_by_id = {v["id"]: v for v in valves}
    bo = blackout_slots(blackouts)
    flow_used = [0] * SLOTS
    valve_used = {v["id"]: [0] * SLOTS for v in valves}
    zones_by_id = {z["id"]: z for z in zones}
    for it in items:
        z = zones_by_id.get(it["zone_id"])
        if not z:
            continue
        for s in zone_run_slots(it["start_slot"], it["length"]):
            if 0 <= s < SLOTS:
                flow_used[s] += z["flow_lmin"]
                valve_used[z["valve_id"]][s] += 1

    vid = zone["valve_id"]
    valve = valve_by_id.get(vid)
    if not valve:
        return ["分区未绑定有效阀门"]
    length = slots_for_water(zone["water_l"], valve["flow_lmin"])

    ew, lw = zone["earliest_slot"], zone["latest_slot"]
    window_slots = [s for s in range(ew, lw) if 0 <= s < SLOTS]
    if len(window_slots) < length:
        return [f"允许时段内仅 {len(window_slots) * SLOT_MIN} 分钟可用，"
                f"至少需要 {length * SLOT_MIN} 分钟"]
    if sum(1 for s in window_slots if s in bo) == len(window_slots):
        return ["允许时段全部处于停水时段"]

    best_start, best_score, best_viol = None, None, None
    for start in range(ew, lw - length + 1):
        run = zone_run_slots(start, length)
        viol = _zone_violations(zone, length, run, bo, total_flow, flow_used,
                                valve_used, valve_by_id)
        score = len(viol)
        if best_score is None or score < best_score:
            best_start, best_score, best_viol = start, score, viol
        if score == 0:
            break

    if best_viol is None:
        return ["允许时段或停水设置无法容纳该时段"]
    return _compress_issues(best_viol, length)


def generate(zones, valves, blackouts, total_flow):
    """贪心编排：按调度紧迫度（窗口-需槽数）升序，依次寻找首个零冲突位置。

    返回 {items:[{zone_id,start_slot,length}], unscheduled:[zone_id,...]}
    """
    valve_by_id = {v["id"]: v for v in valves}
    bo = blackout_slots(blackouts)

    flow_used = [0] * SLOTS
    valve_used = {v["id"]: [0] * SLOTS for v in valves}

    prepared = []
    for z in zones:
        v = valve_by_id.get(z["valve_id"])
        if not v:
            prepared.append((z, 0))
            continue
        length = slots_for_water(z["water_l"], v["flow_lmin"])
        prepared.append((z, length))

    # 紧迫度 = 窗口长度 - 需要槽数（越小越紧）；水量大者优先
    prepared.sort(key=lambda zl: (
        (zl[0]["latest_slot"] - zl[0]["earliest_slot"]) - zl[1],
        -zl[0]["water_l"],
        zl[0]["id"],
    ))

    items = []
    unscheduled = []
    for z, length in prepared:
        v = valve_by_id.get(z["valve_id"])
        if not v:
            unscheduled.append(z["id"])
            continue
        placed = False
        for start in range(z["earliest_slot"], z["latest_slot"] - length + 1):
            run = zone_run_slots(start, length)
            if run & bo:
                continue
            if any(flow_used[s] + z["flow_lmin"] > total_flow for s in run):
                continue
            if any(valve_used[z["valve_id"]][s] + 1 > v["concur"] for s in run):
                continue
            items.append({"zone_id": z["id"], "start_slot": start, "length": length})
            for s in run:
                flow_used[s] += z["flow_lmin"]
                valve_used[z["valve_id"]][s] += 1
            placed = True
            break
        if not placed:
            unscheduled.append(z["id"])

    items.sort(key=lambda it: (it["start_slot"], it["zone_id"]))
    return {"items": items, "unscheduled": unscheduled}
