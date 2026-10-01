"""病害接触追踪核心：按时间重放「确诊 / 到访 / 消毒」事件，推演工具与污染分区之间的传播。

规则
----
- 确诊事件把某分区标记为污染分区（传播源头，立即具有传染性）。
- 工具进入已污染分区即被污染；被污染工具之后到访的分区立即列为暴露。
- 暴露分区经过可配置的潜伏时长（latency_hours）后，才具有传染性、可继续污染其他工具；
  确诊源头分区不受潜伏期限制。
- 工具完成对应该病害的「合格消毒」后，其携带的污染被截断，之后到访不再传播，
  直到再次被污染；不合格消毒不截断。
- 同一时刻按 确诊(0) → 到访(1) → 消毒(2) 的顺序处理，同类事件按事件 ID 排序。
- 循环传播只保留最早暴露；暴露时间相同时保留更短事件链；等长事件链按事件 ID 排序。

每个分区 / 工具在任一时刻只保留一条「最优污染标签」：
    label = (污染时间, 事件链长度, 事件 ID 链元组)
标签按字典序比较，天然实现「最早暴露 + 最短链 + 事件 ID 决胜」。
"""

from datetime import datetime, timedelta

DIAGNOSIS = "diagnosis"
VISIT = "visit"
DISINFECTION = "disinfection"
EVENT_TYPES = (DIAGNOSIS, VISIT, DISINFECTION)
TYPE_ORDER = {DIAGNOSIS: 0, VISIT: 1, DISINFECTION: 2}
TYPE_LABEL = {DIAGNOSIS: "确诊", VISIT: "到访", DISINFECTION: "消毒"}

OPEN = "open"
FROZEN = "frozen"

SEED = "seed"        # 确诊源头分区
EXPOSED = "exposed"  # 经传播暴露的分区
CLEAN = "clean"

_CHAIN_FIELDS = ("id", "type", "ts", "tool_id", "tool_name",
                 "zone_id", "zone_name", "qualified", "note")


# ---------------------------------------------------------------- 时间戳

def parse_ts(raw):
    """解析本地时间戳，返回精确到秒的 'YYYY-MM-DDTHH:MM:SS'。

    拒绝带时区/UTC 后缀的字符串（与水质模块一致，避免跨时区排序歧义）。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("时间必须为字符串（YYYY-MM-DDTHH:MM:SS）")
    text = raw.strip()
    if len(text) == 10:  # 只有日期
        text = text + "T00:00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.upper().endswith(("Z", "UTC")) or "+" in text[10:] or "-" in text[10:]:
        raise ValueError("时间不支持时区后缀，请使用本地时间（YYYY-MM-DDTHH:MM:SS）")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间格式错误：{raw}（应为 YYYY-MM-DDTHH:MM:SS）")
    if dt.tzinfo is not None:
        raise ValueError("时间不支持时区，请使用本地时间")
    return dt.replace(microsecond=0).isoformat()


def _dt(ts):
    return datetime.fromisoformat(ts)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def normalize_latency(raw):
    """潜伏时长（小时）：非负数字，允许 0（暴露后立即传染）。"""
    if raw in (None, ""):
        raise ValueError("缺少潜伏时长")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError("潜伏时长必须为数字（小时）")
    if val != val or val < 0:  # NaN / 负数
        raise ValueError("潜伏时长必须为非负数字")
    if val > 24 * 365 * 10:
        raise ValueError("潜伏时长超出合理范围")
    return val


# ---------------------------------------------------------------- 重放

def replay(events, latency_hours, zones=None, tools=None):
    """按时间重放一个案件的全部事件。

    events: [{id, type, ts, tool_id, tool_name, zone_id, zone_name,
              qualified, note}]
    zones:  现有温室分区 [{id, name}]，用于在结果中补全未暴露分区；
    tools:  现有可复用工具 [{id, name}]。
    返回 {cutoff_ts, zones, quarantine, tools, effects}。
    """
    latency = timedelta(hours=float(latency_hours))
    evs = []
    for e in events:
        evs.append({**e, "_dt": _dt(e["ts"])})
    evs.sort(key=lambda e: (e["_dt"], TYPE_ORDER[e["type"]], e["id"]))
    by_id = {e["id"]: e for e in evs}

    zone_label = {}    # zone_id -> (dt, chain_len, code_tuple)
    tool_label = {}    # tool_id -> (dt, chain_len, code_tuple)；仅保存当前携带的污染
    seed_zones = set()
    effects = []       # [{event_id, actions:[...]}]

    def zname(zid, e):
        return e.get("zone_name") or f"分区#{zid}"

    def uname(uid, e):
        return e.get("tool_name") or f"工具#{uid}"

    for e in evs:
        t, etype = e["_dt"], e["type"]
        zid, uid = e.get("zone_id"), e.get("tool_id")
        actions = []

        if etype == DIAGNOSIS:
            if zid is not None:
                seed_zones.add(zid)
                cand = (t, 1, (e["id"],))
                if zid not in zone_label or cand < zone_label[zid]:
                    if zid in zone_label:
                        actions.append(f"{zname(zid, e)} 已有更早暴露记录，确诊事件不覆盖")
                    else:
                        actions.append(f"{zname(zid, e)} 确诊为污染源头分区（立即具有传染性）")
                    zone_label[zid] = cand

        elif etype == VISIT:
            if uid is None or zid is None:
                actions.append("到访事件缺少工具或分区，已跳过")
            else:
                # 1) 工具携带污染 → 到访分区立即暴露
                if uid in tool_label:
                    _, _, code = tool_label[uid]
                    cand = (t, len(code) + 1, code + (e["id"],))
                    if zid not in zone_label:
                        zone_label[zid] = cand
                        actions.append(
                            f"{uname(uid, e)} 携带污染到访，{zname(zid, e)} 立即列为暴露")
                    elif cand < zone_label[zid]:
                        actions.append(
                            f"{zname(zid, e)} 同时刻存在更短事件链，更新传播链")
                        zone_label[zid] = cand
                    # 工具已污染、分区也已暴露：无新传播

                # 2) 分区污染（已过潜伏期）→ 工具被污染
                if zid in zone_label:
                    zt, _, zcode = zone_label[zid]
                    mature = zid in seed_zones or t >= zt + latency
                    if mature:
                        cand = (t, len(zcode) + 1, zcode + (e["id"],))
                        if uid not in tool_label:
                            tool_label[uid] = cand
                            actions.append(
                                f"{uname(uid, e)} 进入已污染分区 {zname(zid, e)}，被污染")
                        # 工具已携带（不晚的）污染标签，不替换
                    elif uid not in tool_label:
                        mature_at = zt + latency
                        actions.append(
                            f"{zname(zid, e)} 仍在潜伏期中（{_iso(mature_at)} 后才可传染），"
                            f"{uname(uid, e)} 未被污染")
                elif uid not in tool_label:
                    actions.append("双方均无该病害污染，无传播")

        else:  # DISINFECTION
            if uid is None:
                actions.append("消毒事件缺少工具，已跳过")
            elif int(e.get("qualified") or 0) == 1:
                if uid in tool_label:
                    tool_label.pop(uid, None)
                    actions.append(
                        f"{uname(uid, e)} 完成合格消毒，截断其传播链")
                else:
                    actions.append(
                        f"{uname(uid, e)} 合格消毒（当时未携带污染，仅作记录）")
            else:
                actions.append(
                    f"{uname(uid, e)} 消毒不合格，不截断传播"
                    + ("，工具仍携带污染" if uid in tool_label else ""))

        effects.append({"event_id": e["id"], "actions": actions})

    return _build_result(zone_label, tool_label, seed_zones, latency,
                         by_id, zones, tools, evs, effects)


# ---------------------------------------------------------------- 结果组装

def _chain_entries(code, by_id):
    """事件 ID 链 -> 按顺序展开的事件对象（用于渲染最短事件链）。"""
    out = []
    for eid in code:
        e = by_id.get(eid)
        if e is None:
            continue
        out.append({k: e.get(k) for k in _CHAIN_FIELDS})
    return out


def _build_result(zone_label, tool_label, seed_zones, latency, by_id,
                  zones, tools, evs, effects):
    cutoff = max((e["_dt"] for e in evs), default=None)
    zone_names = {z["id"]: z["name"] for z in (zones or [])}
    tool_names = {t["id"]: t["name"] for t in (tools or [])}
    for e in by_id.values():
        if e.get("zone_id") is not None:
            zone_names.setdefault(e["zone_id"], e.get("zone_name") or f"分区#{e['zone_id']}")
        if e.get("tool_id") is not None:
            tool_names.setdefault(e["tool_id"], e.get("tool_name") or f"工具#{e['tool_id']}")

    all_zone_ids = set(zone_names) | set(zone_label)
    zone_rows = []
    for zid in all_zone_ids:
        label = zone_label.get(zid)
        if label is None:
            row = {
                "zone_id": zid, "zone_name": zone_names.get(zid, f"分区#{zid}"),
                "state": CLEAN, "exposed_at": None, "mature_at": None,
                "mature": False, "quarantine": False,
                "chain_length": 0, "chain": [],
            }
        else:
            t, length, code = label
            is_seed = zid in seed_zones
            mature_at = t if is_seed else t + latency
            row = {
                "zone_id": zid, "zone_name": zone_names.get(zid, f"分区#{zid}"),
                "state": SEED if is_seed else EXPOSED,
                "exposed_at": _iso(t), "mature_at": _iso(mature_at),
                "mature": cutoff is not None and cutoff >= mature_at,
                "quarantine": True,  # 暴露即隔离，直至人工解除（页面冻结案件）
                "chain_length": length,
                "chain": _chain_entries(code, by_id),
            }
        zone_rows.append(row)
    # 未暴露分区按名称排；暴露分区按（最早时间、链长、事件 ID）排
    zone_rows.sort(key=lambda r: (
        r["exposed_at"] is None,
        r["exposed_at"] or "",
        r["chain_length"],
        tuple(c["id"] for c in r["chain"]),
        r["zone_id"],
    ))

    quarantine = [{
        "zone_id": r["zone_id"], "zone_name": r["zone_name"],
        "state": r["state"], "exposed_at": r["exposed_at"],
        "mature_at": r["mature_at"], "mature": r["mature"],
        "chain_length": r["chain_length"],
    } for r in zone_rows if r["quarantine"]]

    all_tool_ids = set(tool_names) | set(tool_label)
    tool_rows = []
    for tid in all_tool_ids:
        label = tool_label.get(tid)
        tool_rows.append({
            "tool_id": tid,
            "tool_name": tool_names.get(tid, f"工具#{tid}"),
            "contaminated": label is not None,
            "contaminated_at": _iso(label[0]) if label else None,
            "chain_length": label[1] if label else 0,
            "chain": _chain_entries(label[2], by_id) if label else [],
        })
    tool_rows.sort(key=lambda r: (not r["contaminated"],
                                  r["contaminated_at"] or "", r["tool_id"]))

    return {
        "cutoff_ts": _iso(cutoff) if cutoff else None,
        "latency_hours": latency.total_seconds() / 3600,
        "zones": zone_rows,
        "quarantine": quarantine,
        "tools": tool_rows,
        "effects": effects,
    }
