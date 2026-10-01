"""作物积温（GDD）预测核心：温度截断 + 梯形积分 + 按午夜拆分 + 缺口登记。

计算规则
--------
- 批次配置：基温 Tb、上限温度 Tc、目标积温（℃·日）、允许采样间隔（小时）。
- 每条读数 (ts, temp) 的有效温度先截断到 [Tb, Tc]：
  eff = min(Tc, max(Tb, temp)) − Tb，即高于上限按上限计、低于基温按基温计
  （计 0），有效积温始终非负。
- 相邻读数间隔 ≤ 允许采样间隔才参与积分；间隔超限**只登记缺口、不累计**，
  缺口跨越的时间没有任何积温。
- 有效区间按梯形积分：相邻两点有效温度均值 × 时长（小时）/ 24 = ℃·日；
  区间跨越午夜时按午夜切分，午夜点温度按两端线性插值，切分后的两段分别
  计入相邻两个自然日。
- 首次达标时刻：按时间顺序在每个切分子区间内解积分方程（二分求根），
  精确到秒级。
- 未达标时的外推：取最近 7 个「无缺口完整日」（24 小时全部被有效区间覆盖）
  的日均积温，从最后一条读数时间外推预计达标日期；不足 7 个完整日、或
  日均积温为 0 时明确给出不可预测原因。

补录 / 删除读数后调用方应取该批次**全部读数按时间整体重算**（本模块无状态）。
时间戳与水质 / 病害模块一致，只接受本地时间 ISO 字符串。
"""

from datetime import datetime, timedelta, time

DEFAULT_BASE_TEMP = 10.0
DEFAULT_CAP_TEMP = 30.0
DEFAULT_TARGET_GDD = 150.0
DEFAULT_MAX_GAP_HOURS = 6.0

# 温度的物理合理范围（仅作录入 sanity 校验，截断区间在其内）
TEMP_MIN, TEMP_MAX = -50.0, 80.0
GAP_HOURS_MIN, GAP_HOURS_MAX = 0.05, 24 * 365
TARGET_MIN, TARGET_MAX = 0.0001, 1_000_000.0

DAY_SECONDS = 86400.0
_EPS = 1e-9


# ---------------------------------------------------------------- 时间戳

def parse_ts(raw):
    """解析本地时间戳，返回精确到秒的 'YYYY-MM-DDTHH:MM:SS'（与其他模块一致）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("时间戳必须为字符串（YYYY-MM-DDTHH:MM:SS）")
    text = raw.strip()
    if len(text) == 10:  # 只有日期
        text = text + "T00:00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if "Z" in text.upper() or "+" in text[10:] or "-" in text[10:]:
        raise ValueError("时间戳不支持时区后缀，请使用本地时间（YYYY-MM-DDTHH:MM:SS）")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间戳格式错误：{raw}（应为 YYYY-MM-DDTHH:MM:SS）")
    if dt.tzinfo is not None:
        raise ValueError("时间戳不支持时区，请使用本地时间")
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- 配置 / 读数校验

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
    """校验批次积温参数，返回 {base_temp, cap_temp, target_gdd, max_gap_hours}。"""
    if not isinstance(raw, dict):
        raise ValueError("请求体必须为 JSON 对象")
    base = _num(raw.get("base_temp"), "基温")
    cap = _num(raw.get("cap_temp"), "上限温度")
    target = _num(raw.get("target_gdd"), "目标积温")
    gap_hours = _num(raw.get("max_gap_hours"), "允许采样间隔")
    if not (TEMP_MIN <= base < cap <= TEMP_MAX):
        raise ValueError(
            f"温度参数需满足 {TEMP_MIN:g} ≤ 基温 < 上限温度 ≤ {TEMP_MAX:g}")
    if not (TARGET_MIN <= target <= TARGET_MAX):
        raise ValueError("目标积温必须为正数")
    if not (GAP_HOURS_MIN <= gap_hours <= GAP_HOURS_MAX):
        raise ValueError(
            f"允许采样间隔必须在 {GAP_HOURS_MIN:g} ~ {GAP_HOURS_MAX:g} 小时之间")
    return {
        "base_temp": base, "cap_temp": cap,
        "target_gdd": target, "max_gap_hours": gap_hours,
    }


def validate_readings(batch):
    """校验读数批次：单条对象、数组或 {'batch': [...]} 均可（允许补录历史）。

    每条必须含本地时间戳与温度；批内时间戳重复拒绝。
    返回按时间升序的 [{ts, temp}]。与库内时间戳冲突由调用方靠唯一索引兜底。
    """
    if isinstance(batch, dict) and isinstance(batch.get("batch"), list):
        rows = batch["batch"]
    elif isinstance(batch, list):
        rows = batch
    elif isinstance(batch, dict):
        rows = [batch]
    else:
        raise ValueError("批次必须为读数数组，或 {'batch': [...]} 对象")
    if not rows:
        raise ValueError("批次为空")
    cleaned = []
    seen = set()
    for i, row in enumerate(rows):
        where = f"第 {i + 1} 条"
        if not isinstance(row, dict):
            raise ValueError(f"{where}：读数必须为对象")
        ts = parse_ts(row.get("ts") or row.get("timestamp"))
        temp = _num(row.get("temp"), f"{where}温度")
        if not (TEMP_MIN <= temp <= TEMP_MAX):
            raise ValueError(
                f"{where}温度 {temp:g} 超出物理可能范围（{TEMP_MIN:g}~{TEMP_MAX:g} ℃）")
        if ts in seen:
            raise ValueError(f"{where}时间戳 {ts} 在批次内重复")
        seen.add(ts)
        cleaned.append({"ts": ts, "temp": temp})
    cleaned.sort(key=lambda r: r["ts"])
    return cleaned


# ---------------------------------------------------------------- 计算

def _clamp(temp, base, cap):
    return min(cap, max(base, temp))


def _r3(x):
    """输出用：修约到 0.001，避免浮点尾巴。计算全程仍用全精度。"""
    return round(x + 0.0, 3)


def _solve_hours(v0, v1, hours, rem_dd):
    """在子区间 [0, hours] 内求积温累计到 rem_dd（℃·日）所需的小时数。

    从子区间起点起 x 小时的积温（℃·日）为
        f(x) = (v0*x + (v1-v0)*x²/2) / 24      （梯形面积：平均温度 × 小时 / 24）
    v0、v1 均已截断到 ≥ 基温，f 单调不减，用二分求根。无解返回 None。
    """
    if rem_dd <= _EPS:
        return 0.0

    def f(x):
        # 子区间内 v(t)=v0+(v1-v0)·t/hours；梯形积分换算为 ℃·日
        return (v0 * x + (v1 - v0) * x * x / (2.0 * hours)) / 24.0

    if f(hours) + _EPS < rem_dd:
        return None
    lo, hi = 0.0, float(hours)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if f(mid) < rem_dd:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def compute(config, readings):
    """对一个批次的全部读数按时间整体重算。

    readings: 任意顺序的 [{id?, ts, temp}]（时间戳为本地 ISO）。
    返回结果字典（daily / gaps / 首次达标 / 外推），数值已修约便于序列化。
    """
    base = float(config["base_temp"])
    cap = float(config["cap_temp"])
    target = float(config["target_gdd"])
    max_gap = timedelta(hours=float(config["max_gap_hours"]))

    pts = sorted(
        ((datetime.fromisoformat(parse_ts(r["ts"]))
          if isinstance(r["ts"], str) else r["ts"],
          _clamp(float(r["temp"]), base, cap) - base)
         for r in readings),
        key=lambda p: p[0])

    # day -> 当日统计（全精度）
    days = {}

    def day_row(d):
        return days.setdefault(d, {
            "gdd": 0.0, "covered_seconds": 0.0,
            "gap_seconds": 0.0, "gap_count": 0, "cum_end": 0.0,
        })

    gaps = []
    total = 0.0
    reached_ts = None

    def stamp(d, cum):
        day_row(d)["cum_end"] = cum

    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        delta = t1 - t0
        if delta.total_seconds() <= 0:
            # 同一时间戳的重复读数不参与积分（库内唯一索引也会拦截）
            continue
        if delta.total_seconds() > max_gap.total_seconds() + 1e-6:
            # 间隔超限：只登记缺口，不累计；缺口时长按午夜拆给途经各自然日
            gaps.append({
                "index": len(gaps) + 1,
                "start_ts": t0.isoformat(), "end_ts": t1.isoformat(),
                "duration_hours": _r3(delta.total_seconds() / 3600.0),
            })
            t, t_end = t0, t1
            while t < t_end:
                midnight = datetime.combine(t.date() + timedelta(days=1), time.min)
                t2 = min(t_end, midnight)
                row = day_row(t.date())
                row["gap_seconds"] += (t2 - t).total_seconds()
                row["gap_count"] += 1
                stamp(t.date(), total)
                t = t2
            continue

        # 有效区间：按午夜切分后逐段梯形积分
        t, v = t0, v0
        while t < t1:
            midnight = datetime.combine(t.date() + timedelta(days=1), time.min)
            t2 = min(t1, midnight)
            frac = (t2 - t0).total_seconds() / delta.total_seconds()
            v2 = v0 + (v1 - v0) * frac
            seconds = (t2 - t).total_seconds()
            hours = seconds / 3600.0
            dd = (v + v2) / 2.0 * hours / 24.0  # 梯形面积（℃·小时）/ 24

            if reached_ts is None and total < target - _EPS:
                x = _solve_hours(v, v2, hours, target - total)
                if x is not None:
                    reached_ts = (t + timedelta(hours=x)).replace(microsecond=0)

            row = day_row(t.date())
            row["gdd"] += dd
            row["covered_seconds"] += seconds
            total += dd
            stamp(t.date(), total)
            t, v = t2, v2

    daily = []
    complete_count = 0
    for d in sorted(days):
        row = days[d]
        complete = (row["gap_count"] == 0
                    and row["covered_seconds"] >= DAY_SECONDS - 1e-6)
        complete_count += 1 if complete else 0
        daily.append({
            "date": d.isoformat(),
            "gdd": _r3(row["gdd"]),
            "cumulative_gdd": _r3(row["cum_end"]),
            "covered_hours": _r3(row["covered_seconds"] / 3600.0),
            "gap_hours": _r3(row["gap_seconds"] / 3600.0),
            "gap_count": row["gap_count"],
            "complete": complete,
            "_gdd_raw": row["gdd"],
        })

    first_ts = pts[0][0].isoformat() if pts else None
    last_ts = pts[-1][0].isoformat() if pts else None
    forecast = _forecast(daily, target, total, pts, reached_ts)
    for d in daily:
        d.pop("_gdd_raw", None)
    result = {
        "reading_count": len(pts),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "total_gdd": _r3(total),
        "target_gdd": _r3(target),
        "reached": reached_ts is not None,
        "reached_ts": reached_ts.isoformat() if reached_ts else None,
        "gap_count": len(gaps),
        "gaps": gaps,
        "complete_day_count": complete_count,
        "daily": daily,
        "forecast": forecast,
    }
    return result


def _forecast(daily, target, total, pts, reached_ts):
    """未达标时按最近 7 个无缺口完整日的日均积温外推；不可预测给出原因。"""
    if reached_ts is not None:
        return None
    complete = [d for d in daily if d["complete"]]
    last7 = complete[-7:]
    if len(last7) < 7:
        return {
            "predictable": False,
            "reason": f"无缺口完整日仅 {len(complete)} 个，不足 7 个，样本不足以外推",
            "complete_day_count": len(complete),
            "avg_daily_gdd": None,
            "sample_dates": [d["date"] for d in last7],
            "predicted_date": None,
            "predicted_at": None,
        }
    avg = sum(d["_gdd_raw"] for d in last7) / 7.0
    base = {
        "predictable": False,
        "complete_day_count": len(complete),
        "avg_daily_gdd": _r3(avg),
        "sample_dates": [d["date"] for d in last7],
    }
    if avg <= _EPS:
        return {**base, "reason": "最近 7 个完整日的日均积温为 0（读数均不高于基温），无法外推",
                "predicted_date": None, "predicted_at": None}
    remaining = max(0.0, target - total)
    days_needed = remaining / avg
    predicted_at = (pts[-1][0] + timedelta(days=days_needed)).replace(microsecond=0)
    return {
        **base,
        "predictable": True,
        "reason": None,
        "remaining_gdd": _r3(remaining),
        "days_needed": _r3(days_needed),
        "basis_last_ts": pts[-1][0].isoformat(),
        "predicted_at": predicted_at.isoformat(),
        "predicted_date": predicted_at.date().isoformat(),
    }
