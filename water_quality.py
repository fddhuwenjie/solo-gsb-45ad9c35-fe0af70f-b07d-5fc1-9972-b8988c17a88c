"""灌溉水质连续监测核心：指标区间校验与「连续两次」状态机。

状态规则（按水源、按指标、按采样时间顺序）：
- 采样值连续 2 次落在正常区间外 -> 状态进入「超标」；
- 「超标」后采样值连续 2 次回到区间内 -> 状态恢复「正常」；
- 其余过渡情形 -> 「观察」，并记录触发指标。

单个采样必须包含全部四项指标（pH / EC / 浊度 / 硝酸盐），
各自只接受规定单位（见 METRICS），时间戳必须为本地时间 ISO 字符串。
批量提交时同一批次内时间戳必须严格递增，且不得早于该水源已有最新采样。
"""

# key -> (中文名, 唯一合法单位, 默认下限, 默认上限, 允许最小值, 允许最大值)
METRICS = {
    "ph":        ("pH", "",       5.5, 8.5, 0.0, 14.0),
    "ec":        ("EC", "mS/cm",  0.0, 2.0, 0.0, 1_000_000.0),
    "turbidity": ("浊度", "NTU",  0.0, 10.0, 0.0, 1_000_000.0),
    "nitrate":   ("硝酸盐", "mg/L", 0.0, 30.0, 0.0, 1_000_000.0),
}
METRIC_KEYS = list(METRICS.keys())

NORMAL, OBSERVING, EXCEEDED = "normal", "observing", "exceeded"
STATUS_LABEL = {NORMAL: "正常", OBSERVING: "观察", EXCEEDED: "超标"}
_STATUS_RANK = {NORMAL: 0, OBSERVING: 1, EXCEEDED: 2}


# ---------------------------------------------------------------- 时间戳

def parse_ts(raw):
    """解析本地时间戳，返回精确到秒的 'YYYY-MM-DDTHH:MM:SS'。

    拒绝带时区/UTC 后缀的字符串（避免乱序歧义），拒绝无法解析的值。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("时间戳必须为字符串（YYYY-MM-DDTHH:MM:SS）")
    text = raw.strip()
    # 容忍 "YYYY-MM-DD HH:MM:SS" 写法
    if len(text) == 10:  # 只有日期
        text = text + "T00:00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    up = text.upper()
    if up.endswith(("Z", "UTC")) or "+" in text[10:] or (text[10:].count("-") > 0):
        raise ValueError("时间戳不支持时区后缀，请使用本地时间（YYYY-MM-DDTHH:MM:SS）")
    if hasattr(__import__("datetime"), "datetime"):
        from datetime import datetime
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间戳格式错误：{raw}（应为 YYYY-MM-DDTHH:MM:SS）")
    if dt.tzinfo is not None:
        raise ValueError("时间戳不支持时区，请使用本地时间")
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 区间

def default_ranges():
    return {k: [METRICS[k][2], METRICS[k][3]] for k in METRIC_KEYS}


def normalize_ranges(raw):
    """校验并规整区间配置，返回 {key: (low, high)}。缺项 / 非数字 / 上下限倒置均拒绝。"""
    if not isinstance(raw, dict):
        raise ValueError("指标区间必须为对象")
    out = {}
    for key in METRIC_KEYS:
        label, unit, _, _, abs_min, abs_max = METRICS[key]
        if key not in raw or raw[key] in (None, ""):
            raise ValueError(f"缺少指标区间：{label}")
        pair = raw[key]
        if isinstance(pair, dict):
            lo_s, hi_s = pair.get("low"), pair.get("high")
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            lo_s, hi_s = pair[0], pair[1]
        else:
            raise ValueError(f"{label} 区间必须为 [下限, 上限]")
        try:
            lo, hi = float(lo_s), float(hi_s)
        except (TypeError, ValueError):
            raise ValueError(f"{label} 区间上下限必须为数字")
        if not (lo == lo and hi == hi):  # NaN
            raise ValueError(f"{label} 区间不能为 NaN")
        if not (abs_min <= lo < hi <= abs_max):
            if key == "ph":
                raise ValueError(f"{label} 区间需满足 0 ≤ 下限 < 上限 ≤ 14")
            raise ValueError(f"{label} 区间需满足 0 ≤ 下限 < 上限")
        out[key] = (lo, hi)
    return out


def in_range(value, lo, hi):
    return lo <= value <= hi


# ---------------------------------------------------------------- 采样校验

def _parse_metric_value(metric, entry):
    """从单条指标项中取值并校验单位。entry 支持 {value, unit} 或裸数字。"""
    label, canonical, _, _, abs_min, abs_max = METRICS[metric]
    unit = canonical
    if isinstance(entry, dict):
        if entry.get("value") in (None, ""):
            raise ValueError(f"缺少采样值：{label}")
        value_raw = entry.get("value")
        unit = entry.get("unit", canonical)
        if unit is None:
            unit = canonical
    elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
        value_raw = entry
    else:
        if entry in (None, ""):
            raise ValueError(f"缺少采样项：{label}")
        raise ValueError(f"{label} 采样值必须为数字")
    if str(unit).strip() != canonical:
        shown = canonical if canonical else "无单位（pH 为无量纲值）"
        raise ValueError(f"{label} 的单位非法：{unit}（仅接受 {shown}）")
    try:
        value = float(value_raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 采样值必须为数字")
    if value != value:
        raise ValueError(f"{label} 采样值不能为 NaN")
    if not (abs_min <= value <= abs_max):
        rng = "0~14" if metric == "ph" else "非负数"
        raise ValueError(f"{label} 采样值超出物理可能范围（{rng}）")
    return value


def validate_sample_payload(payload, ranges):
    """校验单条采样：缺项 / 非法单位 / 非数字 / 时间戳错误。

    返回规整后的 dict：{ts, metric: value}。ranges 用于确认四项齐全（区间配置必为全量）。
    """
    if not isinstance(payload, dict):
        raise ValueError("采样必须为对象")
    ts = parse_ts(payload.get("ts") or payload.get("timestamp"))
    values = payload.get("values", payload)
    clean = {"ts": ts}
    for key in METRIC_KEYS:
        if key not in values or values[key] in (None, ""):
            raise ValueError(f"采样缺项：{METRICS[key][0]}")
        clean[key] = _parse_metric_value(key, values[key])
    return clean


def validate_batch(batch, ranges, latest_ts=None):
    """校验批次（数组或带 batch 字段的对象）。

    - 批内时间戳必须严格递增（拒绝乱序、重复）；
    - 首条时间戳不得早于/等于该水源已有最新采样（拒绝乱序批次）。
    返回规整后的 [ {ts, ...values} ]。
    """
    if isinstance(batch, dict) and isinstance(batch.get("batch"), list):
        rows = batch["batch"]
    elif isinstance(batch, list):
        rows = batch
    elif isinstance(batch, dict):
        rows = [batch]  # 单条采样
    else:
        raise ValueError("批次必须为采样数组，或 {'batch': [...]} 对象")
    if not rows:
        raise ValueError("批次为空")
    cleaned = []
    prev = latest_ts
    for i, row in enumerate(rows):
        clean = validate_sample_payload(row, ranges)
        where = f"第 {i + 1} 条"
        if prev is not None and clean["ts"] <= prev:
            raise ValueError(
                f"{where}时间戳 {clean['ts']} 乱序：必须晚于上一条/库内最新时间 {prev}")
        cleaned.append(clean)
        prev = clean["ts"]
    return cleaned


# ---------------------------------------------------------------- 状态机

def compute_metric_series(values, lo, hi):
    """对单一指标的时间序列计算每次采样后的状态。

    返回与 values 等长的 [(status, prev_flag, flag)]，flag 为本次是否在区间内。
    规则：连续两次越界 -> 超标；超标后连续两次回区间 -> 正常；其余为观察。
    """
    status = NORMAL
    prev_flag = True  # 基线视为在区间内：首个越界点为「观察」
    series = []
    for value in values:
        flag = in_range(value, lo, hi)
        if status == NORMAL:
            if not flag:
                status = OBSERVING if prev_flag else EXCEEDED
        elif status == EXCEEDED:
            if flag:
                status = OBSERVING if not prev_flag else NORMAL
            # 再次越界：保持超标（prev_flag 已为 False）
        else:  # OBSERVING：由最近两次是否同向决定落入哪一端
            status = EXCEEDED if not flag and not prev_flag else \
                NORMAL if flag and prev_flag else OBSERVING
        series.append(status)
        prev_flag = flag
    return series


def recompute_source(samples, ranges):
    """samples: 按 ts 升序的 dict 列表；返回带状态的序列与水源汇总。

    每个采样附加：
      metric_states: {key: status}
      status:        水源整体状态（取最差指标）
      triggers:      本次非「正常」的指标中文名列表
    返回 (samples, summary)。
    """
    values_by_key = {k: [s[k] for s in samples] for k in METRIC_KEYS}
    series_by_key = {
        k: compute_metric_series(values_by_key[k], ranges[k][0], ranges[k][1])
        for k in METRIC_KEYS
    }
    for i, s in enumerate(samples):
        mstates = {k: series_by_key[k][i] for k in METRIC_KEYS}
        s["metric_states"] = mstates
        worst = max(METRIC_KEYS, key=lambda k: _STATUS_RANK[mstates[k]])
        s["status"] = mstates[worst]
        s["triggers"] = [METRICS[k][0] for k in METRIC_KEYS if mstates[k] != NORMAL]

    if samples:
        last = samples[-1]
        summary = {
            "status": last["status"],
            "triggers": last["triggers"],
            "last_ts": last["ts"],
            "metric_states": last["metric_states"],
        }
    else:
        summary = {
            "status": NORMAL, "triggers": [], "last_ts": None,
            "metric_states": {k: NORMAL for k in METRIC_KEYS},
        }
    return samples, summary


def merge_status(statuses):
    """多个水源状态合并为总览状态。"""
    if not statuses:
        return NORMAL
    return max(statuses, key=lambda s: _STATUS_RANK[s])
