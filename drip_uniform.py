"""滴灌均匀度（DU）巡检核心：低四分位均值 DU + 供压 / 支管压损 / 堵塞判定。

计算规则（按巡检轮次、按分区）
-----------------------------
- 每个分区配置：基准入口压力、设计滴头流量、DU 下限、尾首降幅阈值、低流量比例。
- 每个分区至少 **8 个**滴头实测流量样本（按首 / 中 / 尾编号）。
- 样本按流量升序排列，取前 ``ceil(n/4)`` 个求**低四分位均值**，
  DU = 低四分位均值 / 全样本均值（mean 为 0 时 DU 记 0）。
- 尾首降幅 = (首段流量中位数 − 尾段流量中位数) / 首段流量中位数。

整轮判定
~~~~~~~~
1. **共享供压异常**：超过半数分区同时满足「入口压力 < 基准入口压力」且
   「DU 合格（≥ 下限）」→ 整轮标记为共享供压异常（源头在泵房 / 干管），
   命中分区标注为共享供压涉及分区，整轮不再单列支管压损 / 滴头堵塞。
2. 未触发共享供压异常时：**入口压力合格、DU 不合格、且尾首降幅 ≥ 阈值**
   的分区判为**支管压损**（尾段低压导致的不均匀，低流量点不算滴头堵塞）。
3. 其余分区中，实测流量 **< 设计滴头流量 × 低流量比例** 的采样点列为
   **疑似滴头堵塞**维护项。

补录或删除样本后调用方应取整轮全部分区**整体重算**（本模块无状态）。
时间戳只接受本地时间 ISO 字符串，与水质 / 病害 / 积温模块一致。
"""

import math
from datetime import datetime

HEAD, MIDDLE, TAIL = "head", "middle", "tail"
POSITIONS = (HEAD, MIDDLE, TAIL)
POS_LABEL = {HEAD: "首段", MIDDLE: "中段", TAIL: "尾段"}

MIN_SAMPLES = 8

# 整轮判定
ROUND_NORMAL = "normal"
ROUND_SHARED = "shared_pressure"
ROUND_LABEL = {ROUND_NORMAL: "正常", ROUND_SHARED: "共享供压异常"}

# 分区判定（local verdict）
ZONE_OK = "ok"                    # 压力合格且 DU 合格
ZONE_PIPE_LOSS = "pipe_loss"      # 支管压损
ZONE_PRESSURE_LOW = "pressure_low"  # 入口压力偏低但 DU 合格（共享供压候选）
ZONE_ABNORMAL = "abnormal"        # DU 不合格且不满足支管压损条件
ZONE_SHARED_HIT = "shared_hit"    # 整轮共享供压异常时的命中分区
ZONE_INSUFFICIENT = "insufficient"  # 样本不足 8 个
ZONE_LABEL = {
    ZONE_OK: "合格",
    ZONE_PIPE_LOSS: "支管压损",
    ZONE_PRESSURE_LOW: "入口压力偏低",
    ZONE_ABNORMAL: "均匀度异常",
    ZONE_SHARED_HIT: "共享供压异常涉及分区",
    ZONE_INSUFFICIENT: "样本不足",
}

# 参数物理合理范围（录入 sanity 校验）
PRESSURE_MIN, PRESSURE_MAX = 0.0, 10.0      # MPa
FLOW_MIN, FLOW_MAX = 0.0, 1_000_000.0       # L/h
RATIO_MIN, RATIO_MAX = 0.0, 1.0


# ---------------------------------------------------------------- 时间戳

def parse_ts(raw):
    """解析本地时间戳，返回精确到秒的 'YYYY-MM-DDTHH:MM:SS'。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("巡检时间必须为字符串（YYYY-MM-DDTHH:MM:SS）")
    text = raw.strip()
    if len(text) == 10:  # 只有日期
        text = text + "T00:00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if "Z" in text.upper() or "+" in text[10:] or "-" in text[10:]:
        raise ValueError("巡检时间不支持时区后缀，请使用本地时间（YYYY-MM-DDTHH:MM:SS）")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间格式错误：{raw}（应为 YYYY-MM-DDTHH:MM:SS）")
    if dt.tzinfo is not None:
        raise ValueError("巡检时间不支持时区，请使用本地时间")
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 校验

def _num(raw, field):
    if raw in (None, ""):
        raise ValueError(f"缺少字段：{field}")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须为数字")
    if val != val:  # NaN
        raise ValueError(f"{field} 不能为 NaN")
    if val in (float("inf"), float("-inf")):
        raise ValueError(f"{field} 必须为有限数字")
    return val


def validate_config(raw):
    """校验单个分区的巡检参数，返回规整后的 dict。

    必含：base_inlet_pressure（基准入口压力 MPa，>0）、
    design_flow（设计滴头流量 L/h，>0）、du_min（DU 下限，0<x≤1）、
    tail_drop_limit（尾首降幅阈值，0≤x<1）、low_flow_ratio（低流量比例，0<x≤1）。
    """
    if not isinstance(raw, dict):
        raise ValueError("分区巡检参数必须为对象")
    base = _num(raw.get("base_inlet_pressure"), "基准入口压力")
    design = _num(raw.get("design_flow"), "设计滴头流量")
    du_min = _num(raw.get("du_min"), "DU 下限")
    tail_limit = _num(raw.get("tail_drop_limit"), "尾首降幅阈值")
    low_ratio = _num(raw.get("low_flow_ratio"), "低流量比例")
    if not (PRESSURE_MIN < base <= PRESSURE_MAX):
        raise ValueError(f"基准入口压力必须在 0~{PRESSURE_MAX:g} MPa 之间且大于 0")
    if not (FLOW_MIN < design <= FLOW_MAX):
        raise ValueError("设计滴头流量必须为正数")
    if not (RATIO_MIN < du_min <= RATIO_MAX):
        raise ValueError("DU 下限必须在 0~1 之间（如 0.8）")
    if not (RATIO_MIN <= tail_limit < RATIO_MAX):
        raise ValueError("尾首降幅阈值必须在 [0,1) 之间（如 0.1）")
    if not (RATIO_MIN < low_ratio <= RATIO_MAX):
        raise ValueError("低流量比例必须在 0~1 之间（如 0.7）")
    return {
        "base_inlet_pressure": base,
        "design_flow": design,
        "du_min": du_min,
        "tail_drop_limit": tail_limit,
        "low_flow_ratio": low_ratio,
    }


def validate_inlet_pressure(raw, required=True):
    """入口压力（MPa）：非负数字；required=False 时允许留空（None 表示未测）。"""
    if raw in (None, ""):
        if required:
            raise ValueError("缺少字段：入口压力")
        return None
    val = _num(raw, "入口压力")
    if not (PRESSURE_MIN <= val <= PRESSURE_MAX):
        raise ValueError(f"入口压力必须在 0~{PRESSURE_MAX:g} MPa 之间")
    return val


def validate_samples(batch):
    """校验样本批次：单条对象、数组或 {'batch': [...]} 均可。

    每条必须含 position（head/middle/tail）、label（滴头编号，非空）、
    flow（实测流量 L/h，非负）；批内同段编号重复拒绝。
    返回 [{position, label, flow}]。与库内编号冲突由调用方靠唯一索引兜底。
    """
    if isinstance(batch, dict) and isinstance(batch.get("batch"), list):
        rows = batch["batch"]
    elif isinstance(batch, list):
        rows = batch
    elif isinstance(batch, dict):
        rows = [batch]
    else:
        raise ValueError("批次必须为样本数组，或 {'batch': [...]} 对象")
    if not rows:
        raise ValueError("批次为空")
    cleaned = []
    seen = set()
    for i, row in enumerate(rows):
        where = f"第 {i + 1} 条"
        if not isinstance(row, dict):
            raise ValueError(f"{where}：样本必须为对象")
        position = (row.get("position") or "").strip()
        if position not in POSITIONS:
            raise ValueError(f"{where}：滴头位置必须为 head（首）/middle（中）/tail（尾）")
        label = str(row.get("label") or "").strip()
        if not label:
            raise ValueError(f"{where}：缺少滴头编号")
        flow = _num(row.get("flow"), f"{where}实测流量")
        if not (FLOW_MIN <= flow <= FLOW_MAX):
            raise ValueError(f"{where}实测流量 {flow:g} 超出物理可能范围（非负数字）")
        key = (position, label)
        if key in seen:
            raise ValueError(f"{where}：{POS_LABEL[position]}编号 {label} 在批次内重复")
        seen.add(key)
        cleaned.append({"position": position, "label": label, "flow": flow})
    return cleaned


# ---------------------------------------------------------------- 计算

def _r3(x):
    """输出用：流量 / 压力修约到 0.001，避免浮点尾巴。"""
    return None if x is None else round(x + 0.0, 3)


def _r4(x):
    """输出用：DU / 比例修约到 0.0001。"""
    return None if x is None else round(x + 0.0, 4)


def _pct(x):
    return None if x is None else round(x * 100, 1)


def _median(values):
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def evaluate_zone(cfg, inlet_pressure, samples):
    """对单个分区的全部样本整体重算。

    cfg：validate_config 规整后的参数；inlet_pressure：入口压力（可为 None）；
    samples：任意顺序的 [{id?, position, label, flow}]。
    返回判定结果字典（数值已修约便于序列化）。
    """
    base = float(cfg["base_inlet_pressure"])
    design = float(cfg["design_flow"])
    du_min = float(cfg["du_min"])
    tail_limit = float(cfg["tail_drop_limit"])
    low_ratio = float(cfg["low_flow_ratio"])

    flows = [float(s["flow"]) for s in samples]
    n = len(flows)
    evaluated = n >= MIN_SAMPLES

    by_pos = {p: [float(s["flow"]) for s in samples if s["position"] == p]
              for p in POSITIONS}
    head_med = _median(by_pos[HEAD])
    middle_med = _median(by_pos[MIDDLE])
    tail_med = _median(by_pos[TAIL])
    if head_med is not None and tail_med is not None and head_med > 0:
        tail_drop = (head_med - tail_med) / head_med
    else:
        tail_drop = None

    mean_flow = low_mean = du = None
    low_k = None
    if evaluated:
        ordered = sorted(flows)
        low_k = math.ceil(n / 4)
        low_vals = ordered[:low_k]
        low_mean = sum(low_vals) / low_k
        mean_flow = sum(flows) / n
        du = low_mean / mean_flow if mean_flow > 0 else 0.0

    pressure_measured = inlet_pressure is not None
    pressure_ok = pressure_measured and inlet_pressure >= base
    pressure_low = pressure_measured and inlet_pressure < base
    du_pass = evaluated and du >= du_min
    tail_drop_pass = tail_drop is not None and tail_drop >= tail_limit
    clog_threshold = design * low_ratio

    clog_points = []
    for s in samples:
        if float(s["flow"]) < clog_threshold:
            clog_points.append({
                "sample_id": s.get("id"),
                "position": s["position"],
                "position_label": POS_LABEL[s["position"]],
                "label": s["label"],
                "flow": _r3(float(s["flow"])),
            })
    clog_points.sort(key=lambda p: (POSITIONS.index(p["position"]), p["label"]))

    # ---- 局部分区判定 ----
    if not evaluated:
        verdict = ZONE_INSUFFICIENT
    elif pressure_ok and not du_pass and tail_drop_pass:
        verdict = ZONE_PIPE_LOSS
    elif pressure_low and du_pass:
        verdict = ZONE_PRESSURE_LOW
    elif du_pass:
        verdict = ZONE_OK
    else:
        verdict = ZONE_ABNORMAL  # DU 不合格但不满足支管压损的全部条件

    reasons = _zone_reasons(
        verdict, n, inlet_pressure, base, pressure_measured, pressure_ok,
        du, du_min, du_pass, head_med, tail_med, tail_drop, tail_limit,
        tail_drop_pass, clog_threshold, clog_points)

    return {
        "sample_count": n,
        "evaluated": evaluated,
        "low_quarter_count": low_k,
        "low_quarter_mean": _r3(low_mean),
        "mean_flow": _r3(mean_flow),
        "du": _r4(du),
        "du_min": _r4(du_min),
        "du_pass": bool(du_pass),
        "inlet_pressure": _r3(inlet_pressure),
        "pressure_measured": bool(pressure_measured),
        "base_inlet_pressure": _r3(base),
        "pressure_ok": bool(pressure_ok),
        "head_median": _r3(head_med),
        "middle_median": _r3(middle_med),
        "tail_median": _r3(tail_med),
        "tail_head_drop": _r4(tail_drop),
        "tail_head_drop_pct": _pct(tail_drop),
        "tail_drop_limit": _r4(tail_limit),
        "tail_drop_pass": bool(tail_drop_pass),
        "design_flow": _r3(design),
        "low_flow_ratio": _r4(low_ratio),
        "clog_threshold": _r3(clog_threshold),
        "shared_hit": bool(evaluated and pressure_low and du_pass),
        "verdict": verdict,
        "verdict_label": ZONE_LABEL[verdict],
        "reasons": reasons,
        "clog_points": clog_points,
    }


def _zone_reasons(verdict, n, inlet, base, pressure_measured, pressure_ok,
                  du, du_min, du_pass, head_med, tail_med, drop,
                  tail_limit, drop_pass, clog_threshold, clog_points):
    """生成面向巡检台账的判定依据（人话）。"""
    reasons = []
    if not pressure_measured:
        reasons.append("入口压力未录入，无法判定供压是否合格（不计入共享供压候选）")
    elif pressure_ok:
        reasons.append(f"入口压力 {inlet:g} ≥ 基准 {base:g} MPa，供压合格")
    else:
        reasons.append(f"入口压力 {inlet:g} < 基准 {base:g} MPa，供压偏低")

    if verdict == ZONE_INSUFFICIENT:
        reasons.append(f"样本仅 {n} 个，不足每区 {MIN_SAMPLES} 个，DU 不计算")
        return reasons

    reasons.append(
        f"DU={du:.4f}（低四分位均值 / 全样本均值），"
        f"{'≥' if du_pass else '<'} 下限 {du_min:g}，DU{'合格' if du_pass else '不合格'}")

    if head_med is not None and tail_med is not None and drop is not None:
        reasons.append(
            f"尾段中位数 {tail_med:g} 相对首段 {head_med:g} 降幅 {drop * 100:.1f}%"
            f"（阈值 {tail_limit * 100:g}%，{'达到' if drop_pass else '未达到'}）")
    else:
        reasons.append("首段 / 尾段缺少样本，无法计算尾首降幅")

    if verdict == ZONE_PIPE_LOSS:
        reasons.append("供压合格但 DU 不合格且尾首降幅达到阈值 → 判为支管压损，"
                       "尾段低流量点按压损解释，不列滴头堵塞")
    elif verdict == ZONE_PRESSURE_LOW:
        reasons.append("供压偏低但 DU 仍合格：计入共享供压异常候选分区")
    elif verdict == ZONE_ABNORMAL:
        reasons.append("DU 不合格但尾首降幅未达阈值，未匹配支管压损，需现场复核")
    if clog_points and verdict not in (ZONE_PIPE_LOSS,):
        reasons.append(f"{len(clog_points)} 个采样点流量 < 堵塞阈值 "
                       f"{clog_threshold:g} L/h，列为疑似滴头堵塞")
    return reasons


# ---------------------------------------------------------------- 整轮

def evaluate_round(zone_payloads):
    """对一整轮巡检的全部分区整体重算。

    zone_payloads: [{drip_zone_id, zone_id, zone_name, inlet_pressure,
                     **cfg, samples: [{id, position, label, flow}]}]
    返回 {verdict, zones: [每区结果 + 标识], maintenance: 维护清单, ...计数}。
    """
    zones = []
    for p in zone_payloads:
        cfg = {k: p[k] for k in
               ("base_inlet_pressure", "design_flow", "du_min",
                "tail_drop_limit", "low_flow_ratio")}
        r = evaluate_zone(cfg, p.get("inlet_pressure"), p.get("samples") or [])
        r.update({
            "drip_zone_id": p.get("drip_zone_id"),
            "zone_id": p.get("zone_id"),
            "zone_name": p.get("zone_name"),
        })
        zones.append(r)

    hits = [z for z in zones if z["shared_hit"]]
    total = len(zones)
    shared = total > 0 and len(hits) * 2 > total  # 严格超过半数
    hit_ids = {z["drip_zone_id"] for z in hits}

    if shared:
        verdict = ROUND_SHARED
        for z in zones:
            if z["drip_zone_id"] in hit_ids:
                z["verdict"] = ZONE_SHARED_HIT
                z["verdict_label"] = ZONE_LABEL[ZONE_SHARED_HIT]
                z["reasons"].append(
                    "整轮超过半数分区供压偏低且 DU 合格 → 命中共享供压异常，"
                    "源头在泵房 / 干管共享供压")
        rule = (f"{len(hits)}/{total} 个分区同时满足入口压力低于基准且 DU 合格，"
                f"超过半数（{total // 2}）→ 整轮标记共享供压异常")
        maintenance = [{
            "type": "shared_pressure",
            "priority": "high",
            "drip_zone_id": None,
            "zone_id": None,
            "zone_name": None,
            "action": "检查泵房供水压力、干管阀门开度与过滤器 / 施肥罐前后压差，"
                      "恢复共享供压后复测",
            "basis": rule,
        }]
    else:
        verdict = ROUND_NORMAL
        rule = (f"供压偏低且 DU 合格的分区 {len(hits)}/{total} 个，"
                "未超过半数 → 不触发共享供压异常，按分区各自判定")
        maintenance = _local_maintenance(zones)

    evaluated = [z for z in zones if z["evaluated"]]
    return {
        "verdict": verdict,
        "verdict_label": ROUND_LABEL[verdict],
        "zone_count": total,
        "evaluated_count": len(evaluated),
        "du_pass_count": sum(1 for z in evaluated if z["du_pass"]),
        "shared_hit_count": len(hits),
        "shared_triggered": bool(shared),
        "rule": rule,
        "zones": zones,
        "maintenance": maintenance,
    }


def _local_maintenance(zones):
    """未触发共享供压异常时的分区级维护清单。"""
    items = []
    for z in zones:
        name = z["zone_name"]
        dzid, zid = z["drip_zone_id"], z["zone_id"]
        v = z["verdict"]
        if v == ZONE_INSUFFICIENT:
            items.append({
                "type": "insufficient", "priority": "low",
                "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                "action": f"补录「{name}」滴头样本至每区至少 {MIN_SAMPLES} 个后重算",
                "basis": f"当前仅 {z['sample_count']} 个样本，DU 不计算",
            })
            continue
        if not z.get("pressure_measured", True):
            items.append({
                "type": "pressure_missing", "priority": "low",
                "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                "action": f"补测「{name}」入口压力并录入，以确认供压是否合格",
                "basis": "入口压力未录入，未参与供压与共享供压判定",
            })
        if v == ZONE_PIPE_LOSS:
            items.append({
                "type": "pipe_loss", "priority": "high",
                "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                "action": f"沿「{name}」支管冲洗 / 检漏，检查尾端阀门与管路弯折、破损",
                "basis": f"供压合格但 DU={z['du']:.4f} 低于下限 {z['du_min']:g}，"
                         f"尾首降幅 {z['tail_head_drop_pct']}% 达到阈值 "
                         f"{z['tail_drop_limit'] * 100:g}%",
            })
        if v == ZONE_PRESSURE_LOW:
            items.append({
                "type": "pressure", "priority": "low",
                "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                "action": f"核查「{name}」入口阀门开度、过滤器压差与入口压力表",
                "basis": f"入口压力 {z['inlet_pressure']:g} < 基准 "
                         f"{z['base_inlet_pressure']:g} MPa，DU 尚合格",
            })
        if v == ZONE_ABNORMAL:
            items.append({
                "type": "du_fail", "priority": "medium",
                "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                "action": f"现场复核「{name}」滴头堵塞与支管工况，必要时分段测压",
                "basis": f"DU={z['du']:.4f} 低于下限 {z['du_min']:g}，"
                         f"尾首降幅 {z['tail_head_drop_pct']}% 未达支管压损阈值",
            })
        # 支管压损分区的低流量点是尾段压损所致，不计滴头堵塞
        if v != ZONE_PIPE_LOSS:
            for p in z["clog_points"]:
                items.append({
                    "type": "clog", "priority": "medium",
                    "drip_zone_id": dzid, "zone_id": zid, "zone_name": name,
                    "action": f"检查 / 清洗「{name}」{p['position_label']}{p['label']} 号滴头",
                    "basis": f"实测流量 {p['flow']:g} < 设计 {z['design_flow']:g}"
                             f"×{z['low_flow_ratio']:g} = {z['clog_threshold']:g} L/h",
                    "sample_id": p["sample_id"],
                    "position": p["position"],
                    "label": p["label"],
                })
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda it: (priority_rank[it["priority"]],
                               it.get("zone_name") or "", it.get("label") or ""))
    return items
