/* 作物积温预测 —— 原生 JS */
(() => {
  "use strict";

  let state = null;
  const filter = { zone_id: null, batch_id: null };

  const $ = (sel, root = document) => root.querySelector(sel);
  const NS = "http://www.w3.org/2000/svg";

  // ------------------------------------------------------------ 工具

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 3000);
  }

  async function api(path, options = {}) {
    const opts = { headers: {}, ...options };
    if (opts.body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) throw new Error((data && data.error) || `请求失败 (${res.status})`);
    return data;
  }

  const escapeHtml = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtTs = (ts) => String(ts || "").replace("T", " ");
  const fmtNum = (v, d = 2) =>
    (v === null || v === undefined) ? "—" : (Math.round(v * 10 ** d) / 10 ** d).toString();

  function clampTemp(temp, sel) {
    return Math.min(sel.cap_temp, Math.max(sel.base_temp, temp));
  }

  function nowLocalInput() {
    const d = new Date();
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
      `T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // ------------------------------------------------------------ 加载

  function stateUrl(extra = {}) {
    const p = new URLSearchParams();
    const bid = extra.batch_id ?? filter.batch_id;
    const zid = extra.zone_id ?? filter.zone_id;
    if (bid) p.set("batch_id", bid);
    if (zid) p.set("zone_id", zid);
    const qs = p.toString();
    return "/api/heat/state" + (qs ? "?" + qs : "");
  }

  async function load(extra) {
    state = await api(stateUrl(extra));
    if (state.selected_id) filter.batch_id = state.selected_id;
    renderAll();
  }

  function renderAll() {
    renderSelectors();
    renderBatchList();
    renderBatchForm();
    renderOverview();
    renderCharts();
    renderGaps();
    renderLedger();
    renderReadings();
  }

  // ------------------------------------------------------------ 批次列表 / 选择器

  const visibleBatches = () => state.batches;

  function renderBatchList() {
    const ul = $("#batch-list");
    ul.innerHTML = "";
    const list = visibleBatches();
    if (!list.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">` +
        (state.zones.length ? "暂无批次，请在下方新建" : "请先在轮灌编排台建立温室分区") + `</li>`;
      return;
    }
    const zname = (id) => (state.zones.find((z) => z.id === id) || {}).name || `分区#${id}`;
    list.forEach((b) => {
      const res = b.result;
      const li = document.createElement("li");
      li.className = "entity-row batch-item" +
        (b.id === state.selected_id ? " selected" : "");
      li.innerHTML =
        `<span class="line1">
           <span class="grow name">${escapeHtml(b.name)}</span>
           <span class="zone-chip">${escapeHtml(zname(b.zone_id))}</span>
         </span>
         <span class="meta">
           ${escapeHtml(b.crop || "未填作物")} · 基温 ${fmtNum(b.base_temp, 1)} / 上限 ${fmtNum(b.cap_temp, 1)} ℃
         </span>
         <span class="meta">
           累计 <b>${fmtNum(res.total_gdd)}</b> / ${fmtNum(b.target_gdd, 1)} ℃·日
           ${res.reached
             ? `· ✅ ${fmtTs(res.reached_ts)} 达标`
             : `· ${res.gap_count ? `⚠ ${res.gap_count} 个缺口 · ` : ""}${res.complete_day_count} 个完整日`}
         </span>`;
      li.addEventListener("click", () => selectBatch(b.id));
      ul.appendChild(li);
    });
  }

  function renderSelectors() {
    const fz = $("#filter-zone"), fb = $("#filter-batch"), bz = $("#batch-zone");
    fz.innerHTML = `<option value="">全部分区</option>` +
      state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("");
    fz.value = state.filter_zone_id || "";

    const list = visibleBatches();
    fb.innerHTML = list.length
      ? list.map((b) =>
          `<option value="${b.id}"${b.id === state.selected_id ? " selected" : ""}>` +
          `${escapeHtml(b.name)}</option>`).join("")
      : `<option value="">（无批次）</option>`;

    bz.innerHTML = state.zones.length
      ? state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("")
      : `<option value="">（请先建立分区）</option>`;

    $("#reading-card").style.opacity = state.selected_id ? "1" : ".55";
  }

  function renderBatchForm() {
    const f = $("#batch-form");
    const sel = state.selected;
    $("#btn-batch-delete").disabled = !sel;
    if (!sel) {
      f.elements.id.value = "";
      $("#batch-submit").textContent = "新建批次";
      $("#batch-cancel").classList.add("hidden");
      return;
    }
    f.elements.id.value = sel.id;
    f.elements.name.value = sel.name;
    f.elements.crop.value = sel.crop || "";
    f.elements.zone_id.value = sel.zone_id;
    f.elements.base_temp.value = sel.base_temp;
    f.elements.cap_temp.value = sel.cap_temp;
    f.elements.target_gdd.value = sel.target_gdd;
    f.elements.max_gap_hours.value = sel.max_gap_hours;
    $("#batch-submit").textContent = "保存参数并重算";
    $("#batch-cancel").classList.remove("hidden");
  }

  async function selectBatch(id) {
    filter.batch_id = id;
    try { await load(); } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 总览 / 外推

  function renderOverview() {
    const sel = state.selected;
    const title = $("#heat-title"), badge = $("#heat-badge");
    const meta = $("#heat-meta"), stats = $("#heat-stats"), fcBox = $("#forecast-box");
    stats.innerHTML = ""; fcBox.innerHTML = ""; badge.className = "case-badge";
    if (!sel) {
      title.textContent = "未选择批次";
      badge.textContent = "";
      meta.textContent = "在左侧新建批次或从筛选器中选择批次。";
      return;
    }
    const res = sel.result;
    const zname = sel.zone_name || `分区#${sel.zone_id}`;
    title.textContent = `${sel.name}（${sel.crop || "未填作物"} · ${zname}）`;
    if (res.reached) {
      badge.classList.add("reached");
      badge.textContent = "已达标";
    } else {
      badge.classList.add("pending");
      badge.textContent = "未达标";
    }
    meta.innerHTML =
      `基温 <b>${fmtNum(sel.base_temp, 1)} ℃</b> · 上限 <b>${fmtNum(sel.cap_temp, 1)} ℃</b>` +
      ` · 目标积温 <b>${fmtNum(sel.target_gdd, 1)} ℃·日</b>` +
      ` · 允许采样间隔 <b>${fmtNum(sel.max_gap_hours, 1)} h</b>` +
      ` · 读数 ${res.reading_count} 条 · 时间范围 ${fmtTs(res.first_ts) || "—"} ~ ${fmtTs(res.last_ts) || "—"}`;

    const items = [
      ["累计积温", fmtNum(res.total_gdd), "℃·日"],
      ["目标积温", fmtNum(sel.target_gdd, 1), "℃·日"],
      ["距目标", fmtNum(Math.max(0, sel.target_gdd - res.total_gdd)), "℃·日"],
      ["缺口数量", res.gap_count, "个"],
      ["无缺口完整日", res.complete_day_count, "天"],
    ];
    if (res.reached) {
      items.push(["首次达标时刻", fmtTs(res.reached_ts), ""]);
    }
    items.forEach(([label, val, unit]) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="stat-label">${label}</span>` +
        `<span class="stat-val">${escapeHtml(String(val))} <span class="stat-unit">${unit}</span></span>`;
      stats.appendChild(li);
    });

    const fc = res.forecast;
    if (res.reached) {
      fcBox.className = "forecast-box done";
      fcBox.innerHTML = `✅ 已于 <strong>${fmtTs(res.reached_ts)}</strong> 首次达到目标积温，无需外推。`;
    } else if (fc && fc.predictable) {
      fcBox.className = "forecast-box ok";
      fcBox.innerHTML =
        `📈 预计 <strong>${fc.predicted_date}</strong> 达标` +
        `（预计时刻 ${fmtTs(fc.predicted_at)}）` +
        `<div class="fc-detail">依据最近 7 个无缺口完整日（${fc.sample_dates[0]} ~ ${fc.sample_dates[6]}）` +
        `日均积温 <b>${fmtNum(fc.avg_daily_gdd)} ℃·日</b>，距目标还差 ${fmtNum(fc.remaining_gdd)} ℃·日，` +
        `约需 ${fmtNum(fc.days_needed, 1)} 天（自最后读数 ${fmtTs(fc.basis_last_ts)} 起线性外推）。</div>`;
    } else if (fc) {
      fcBox.className = "forecast-box no";
      fcBox.innerHTML = `⛔ <strong>不可预测</strong>：${escapeHtml(fc.reason)}。` +
        (fc.sample_dates.length
          ? `<div class="fc-detail">候选完整日：${fc.sample_dates.join("、")}</div>` : "");
    }
  }

  // ------------------------------------------------------------ 曲线（内联 SVG）

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function renderCharts() {
    const wrap = $("#heat-charts"), subtitle = $("#chart-subtitle");
    wrap.innerHTML = "";
    const sel = state.selected;
    if (!sel) {
      subtitle.textContent = "";
      wrap.innerHTML = `<div class="chart-empty">请先选择批次</div>`;
      return;
    }
    const readings = sel.result.readings;
    if (!readings.length) {
      subtitle.textContent = "";
      wrap.innerHTML = `<div class="chart-empty">暂无温度读数，请在左侧录入</div>`;
      return;
    }
    subtitle.textContent =
      `蓝线实测温度，绿色虚线为截断后有效温度（加基温）；` +
      `灰/红虚线为基温/上限，红色阴影为缺口区间`;
    wrap.appendChild(tempChart(sel, readings));
    wrap.appendChild(cumChart(sel));
    wrap.appendChild(barChart(sel));
  }

  function parseT(ts) { return new Date(ts.replace(" ", "T")).getTime(); }

  function tempChart(sel, readings) {
    const W = 900, H = 170, PL = 38, PR = 12, PT = 12, PB = 22;
    const t0 = parseT(readings[0].ts), t1 = parseT(readings[readings.length - 1].ts);
    const span = Math.max(1, t1 - t0);
    const xAt = (t) => PL + (t - t0) / span * (W - PL - PR);

    const temps = readings.map((r) => r.temp);
    let lo = Math.min(...temps, sel.base_temp);
    let hi = Math.max(...temps, sel.cap_temp);
    const pad = Math.max(1, (hi - lo) * 0.12);
    lo -= pad; hi += pad;
    const yAt = (v) => PT + (hi - v) * (H - PT - PB) / (hi - lo);

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` });

    // hatch pattern
    const defs = el("defs", {});
    const pat = el("pattern", { id: "gapHatch", width: 8, height: 8,
      patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    pat.appendChild(el("rect", { width: 8, height: 8, fill: "rgba(198,40,40,.10)" }));
    pat.appendChild(el("line", { x1: 0, y1: 0, x2: 0, y2: 8,
      stroke: "rgba(198,40,40,.35)", "stroke-width": 2 }));
    defs.appendChild(pat); svg.appendChild(defs);

    // 缺口阴影
    sel.result.gaps.forEach((g) => {
      const x = xAt(parseT(g.start_ts)), w = xAt(parseT(g.end_ts)) - x;
      svg.appendChild(el("rect", { x, y: PT, width: Math.max(1, w),
        height: H - PT - PB, class: "gap-band" }));
      svg.appendChild(el("rect", { x, y: PT, width: Math.max(1, w),
        height: H - PT - PB, class: "gap-band-pattern" }));
    });

    // 基温 / 上限参考线
    [["ref-base", sel.base_temp, `基温 ${fmtNum(sel.base_temp, 1)}`],
     ["ref-cap", sel.cap_temp, `上限 ${fmtNum(sel.cap_temp, 1)}`]].forEach(([cls, v, txt]) => {
      const y = yAt(v);
      svg.appendChild(el("line", { x1: PL, x2: W - PR, y1: y, y2: y, class: cls }));
      const t = el("text", { x: PL + 2, y: y - 2, class: "chart-label ref" });
      t.textContent = txt; svg.appendChild(t);
    });

    // 实测温度折线（相邻读数直接相连，视觉上可跨过缺口）
    const rawPts = readings.map((r) => `${xAt(parseT(r.ts))},${yAt(r.temp)}`).join(" ");
    svg.appendChild(el("polyline", { points: rawPts, class: "temp-line" }));

    // 截断后有效温度（eff+base）折线
    const effPts = readings.map((r) =>
      `${xAt(parseT(r.ts))},${yAt(clampTemp(r.temp, sel))}`).join(" ");
    svg.appendChild(el("polyline", { points: effPts, class: "eff-line" }));

    readings.forEach((r) => {
      const clamped = r.temp < sel.base_temp - 1e-9 || r.temp > sel.cap_temp + 1e-9;
      const c = el("circle", {
        cx: xAt(parseT(r.ts)), cy: yAt(r.temp), r: 3,
        class: "temp-pt" + (clamped ? " clamped" : ""),
      });
      c.appendChild(el("title", {})).textContent =
        `${fmtTs(r.ts)}\n实测 ${fmtNum(r.temp, 1)} ℃` +
        (clamped ? `\n超出 [${sel.base_temp}, ${sel.cap_temp}]，按 ${fmtNum(clampTemp(r.temp, sel), 1)} ℃ 计` : "");
      svg.appendChild(c);
    });

    // 首末时间轴
    const ta = el("text", { x: PL, y: H - 6, class: "chart-label" });
    ta.textContent = readings[0].ts.slice(5, 16).replace("T", " "); svg.appendChild(ta);
    const tb = el("text", { x: W - PR, y: H - 6, "text-anchor": "end", class: "chart-label" });
    tb.textContent = readings[readings.length - 1].ts.slice(5, 16).replace("T", " ");
    svg.appendChild(tb);

    const box = document.createElement("div");
    box.className = "chart-box-wide";
    box.innerHTML = `<h3><span>温度曲线（${readings.length} 条读数）</span>` +
      `<small>红点为被截断读数</small></h3>`;
    box.appendChild(svg);
    return box;
  }

  function cumChart(sel) {
    const daily = sel.result.daily;
    const W = 900, H = 170, PL = 46, PR = 14, PT = 14, PB = 22;
    const n = daily.length;
    const xAt = (i) => PL + (n === 1 ? (W - PL - PR) / 2
      : i * (W - PL - PR) / (n - 1));
    let hi = Math.max(sel.target_gdd, ...daily.map((d) => d.cumulative_gdd)) * 1.08;
    hi = Math.max(hi, 1);
    const yAt = (v) => PT + (hi - v) * (H - PT - PB) / hi;

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` });

    // 目标线
    const yt = yAt(sel.target_gdd);
    svg.appendChild(el("line", { x1: PL, x2: W - PR, y1: yt, y2: yt, class: "cum-target" }));
    const tlab = el("text", { x: PL + 2, y: yt - 2, class: "chart-label ref" });
    tlab.textContent = `目标 ${fmtNum(sel.target_gdd, 1)} ℃·日`;
    svg.appendChild(tlab);

    // 累计折线
    const pts = daily.map((d, i) => `${xAt(i)},${yAt(d.cumulative_gdd)}`).join(" ");
    svg.appendChild(el("polyline", { points: pts, class: "cum-line" }));

    daily.forEach((d, i) => {
      const c = el("circle", { cx: xAt(i), cy: yAt(d.cumulative_gdd),
        r: 3, fill: d.gap_count ? "#f59e0b" : "var(--green-dark)", stroke: "#fff", "stroke-width": 1 });
      c.appendChild(el("title", {})).textContent =
        `${d.date}\n累计 ${fmtNum(d.cumulative_gdd)} ℃·日` +
        (d.gap_count ? `\n当日缺口 ${d.gap_hours} h` : "") +
        (d.complete ? "\n无缺口完整日" : "");
      svg.appendChild(c);
    });

    // 首次达标标记
    if (sel.result.reached_ts) {
      const rday = sel.result.reached_ts.slice(0, 10);
      const idx = daily.findIndex((d) => d.date === rday);
      if (idx >= 0) {
        const x = xAt(idx);
        svg.appendChild(el("line", { x1: x, x2: x, y1: PT, y2: H - PB,
          stroke: "var(--red)", "stroke-width": 1, "stroke-dasharray": "3 3" }));
        const m = el("circle", { cx: x, cy: yt, r: 4, class: "reach-marker" });
        svg.appendChild(m);
        const mt = el("text", { x, y: PT + 8, "text-anchor": "middle", class: "chart-label" });
        mt.textContent = `达标 ${fmtTs(sel.result.reached_ts).slice(5)}`;
        svg.appendChild(mt);
      }
    }

    // 外推预计日期标记
    const fc = sel.result.forecast;
    if (fc && fc.predictable) {
      const m = el("text", { x: W - PR, y: H - 6, "text-anchor": "end", class: "chart-label" });
      m.textContent = `预计达标 ${fc.predicted_date}`;
      svg.appendChild(m);
    }

    const step = Math.max(1, Math.ceil(n / 12));
    daily.forEach((d, i) => {
      if (i % step !== 0 && i !== n - 1) return;
      const t = el("text", { x: xAt(i), y: H - 6, "text-anchor": "middle", class: "chart-label" });
      t.textContent = d.date.slice(5);
      svg.appendChild(t);
    });

    const box = document.createElement("div");
    box.className = "chart-box-wide";
    box.innerHTML = `<h3><span>累计积温曲线（按日）</span>` +
      `<small>${sel.result.reached ? "红点竖线为首次达标时刻" : "橙色点为含缺口日期"}</small></h3>`;
    box.appendChild(svg);
    return box;
  }

  function barChart(sel) {
    const daily = sel.result.daily;
    const W = 900, H = 170, PL = 40, PR = 12, PT = 12, PB = 30;
    const n = daily.length;
    const bw = (W - PL - PR) / n;
    const hi = Math.max(1, ...daily.map((d) => d.gdd)) * 1.15;
    const yAt = (v) => PT + (hi - v) * (H - PT - PB) / hi;

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` });
    daily.forEach((d, i) => {
      const x = PL + i * bw + bw * 0.15, w = bw * 0.7;
      const y = yAt(d.gdd), h = H - PB - y;
      const rect = el("rect", { x, y, width: w, height: Math.max(0, h),
        class: "bar-day" + (d.gap_count ? " gapday" : "") });
      rect.appendChild(el("title", {})).textContent =
        `${d.date}\n当日积温 ${fmtNum(d.gdd)} ℃·日` +
        (d.gap_count ? `\n缺口 ${d.gap_hours} h（该时段不累计）` : "") +
        (d.complete ? "\n无缺口完整日（计入外推样本）" : "");
      svg.appendChild(rect);
      if (bw > 30) {
        const t = el("text", { x: x + w / 2, y: H - 8, "text-anchor": "middle",
          class: "chart-label", transform: `rotate(40 ${x + w / 2} ${H - 8})` });
        t.textContent = d.date.slice(5);
        svg.appendChild(t);
      }
    });
    const t = el("text", { x: PL, y: H - 16, class: "chart-label" });
    t.textContent = "绿色=完整日，黄色=含缺口日";
    svg.appendChild(t);

    const box = document.createElement("div");
    box.className = "chart-box-wide";
    box.innerHTML = `<h3><span>每日积温柱状图</span>` +
      `<small>最近 7 个完整日的均值用于未达标时的外推</small></h3>`;
    box.appendChild(svg);
    return box;
  }

  // ------------------------------------------------------------ 缺口清单

  function renderGaps() {
    const card = $("#gap-card"), tbody = $("#gap-rows");
    tbody.innerHTML = "";
    const sel = state.selected;
    const gaps = sel ? sel.result.gaps : [];
    card.classList.toggle("hidden", !gaps.length);
    if (!gaps.length) return;
    gaps.forEach((g) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${g.index}</td><td>${fmtTs(g.start_ts)}</td><td>${fmtTs(g.end_ts)}</td>` +
        `<td class="gap-cell">${fmtNum(g.duration_hours, 2)}</td>` +
        `<td>${fmtNum(sel.max_gap_hours, 2)}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 日台账

  function renderLedger() {
    const tbody = $("#daily-rows"), subtitle = $("#ledger-subtitle");
    tbody.innerHTML = "";
    const sel = state.selected;
    if (!sel) { subtitle.textContent = ""; return; }
    const daily = [...sel.result.daily].reverse();  // 最近日期在前
    subtitle.textContent =
      `共 ${daily.length} 天 · ${sel.result.complete_day_count} 个无缺口完整日`;
    if (!daily.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="6">暂无台账数据</td></tr>`;
      return;
    }
    const rday = sel.result.reached_ts ? sel.result.reached_ts.slice(0, 10) : null;
    daily.forEach((d) => {
      const tr = document.createElement("tr");
      if (rday && d.date === rday) tr.classList.add("reach-row");
      tr.innerHTML =
        `<td>${d.date}${rday && d.date === rday ? " ⭐首次达标日" : ""}</td>` +
        `<td>${fmtNum(d.gdd)}</td><td><b>${fmtNum(d.cumulative_gdd)}</b></td>` +
        `<td>${fmtNum(d.covered_hours, 1)}</td>` +
        `<td class="${d.gap_count ? "gap-cell" : ""}">${d.gap_count ? fmtNum(d.gap_hours, 1) + `（${d.gap_count} 段）` : "—"}</td>` +
        `<td class="${d.complete ? "complete-yes" : "complete-no"}">${d.complete ? "✔ 完整" : "—"}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 读数明细

  function renderReadings() {
    const tbody = $("#reading-rows"), subtitle = $("#reading-subtitle");
    tbody.innerHTML = "";
    const sel = state.selected;
    if (!sel) { subtitle.textContent = ""; return; }
    const readings = [...sel.result.readings].reverse();  // 最新在前
    subtitle.textContent = `共 ${readings.length} 条`;
    if (!readings.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="4">暂无读数</td></tr>`;
      return;
    }
    readings.forEach((r) => {
      const eff = clampTemp(r.temp, sel) - sel.base_temp;
      const clamped = r.temp < sel.base_temp - 1e-9 || r.temp > sel.cap_temp + 1e-9;
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${fmtTs(r.ts)}</td>` +
        `<td class="${clamped ? "gap-cell" : ""}">${fmtNum(r.temp, 1)}</td>` +
        `<td>${fmtNum(eff, 1)}（有效温度，基温之上）</td>` +
        `<td><button class="del-btn" title="删除该读数（删除后全量重算）">🗑</button></td>`;
      tr.querySelector(".del-btn").addEventListener("click", () => removeReading(r));
      tbody.appendChild(tr);
    });
  }

  async function removeReading(r) {
    if (!confirm(`删除 ${fmtTs(r.ts)} 的温度读数？删除后将按时间全量重算。`)) return;
    try {
      await api(`/api/heat/readings/${r.id}`, { method: "DELETE" });
      await load();
      toast("读数已删除，已全量重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 表单

  function bindForms() {
    $("#batch-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        name: f.elements.name.value.trim(),
        crop: f.elements.crop.value.trim(),
        zone_id: Number(f.elements.zone_id.value),
        base_temp: Number(f.elements.base_temp.value),
        cap_temp: Number(f.elements.cap_temp.value),
        target_gdd: Number(f.elements.target_gdd.value),
        max_gap_hours: Number(f.elements.max_gap_hours.value),
      };
      const id = f.elements.id.value;
      try {
        if (id) {
          await api(`/api/heat/batches/${id}`, { method: "PUT", body });
          toast("批次参数已保存，全部读数已重算");
        } else {
          const s = await api("/api/heat/batches", { method: "POST", body });
          filter.batch_id = s.selected_id;
          toast("批次已创建");
        }
        await load();
      } catch (err) { toast(err.message, true); }
    });

    $("#batch-cancel").addEventListener("click", () => {
      $("#batch-form").reset();
      $("#batch-form").elements.id.value = "";
      $("#batch-submit").textContent = "新建批次";
      $("#batch-cancel").classList.add("hidden");
    });

    $("#btn-batch-delete").addEventListener("click", async () => {
      const sel = state.selected;
      if (!sel) return;
      if (!confirm(`删除批次「${sel.name}」？其全部温度读数将一并删除。`)) return;
      try {
        await api(`/api/heat/batches/${sel.id}`, { method: "DELETE" });
        filter.batch_id = null;
        await load();
        toast("批次已删除");
      } catch (err) { toast(err.message, true); }
    });

    $("#reading-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.selected_id) { toast("请先选择批次", true); return; }
      const f = e.target;
      const ts = f.elements.ts.value;
      const temp = Number(f.elements.temp.value);
      try {
        await api(`/api/heat/batches/${state.selected_id}/readings`, {
          method: "POST", body: { ts, temp },
        });
        f.elements.temp.value = "";
        await load();
        toast("读数已录入，已按时间全量重算");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-reading-now").addEventListener("click", () => {
      $("#reading-form").elements.ts.value = nowLocalInput();
    });
  }

  // ------------------------------------------------------------ 筛选 / 导出

  function bindFilters() {
    $("#filter-zone").addEventListener("change", async () => {
      filter.zone_id = Number($("#filter-zone").value) || null;
      filter.batch_id = null;  // 交给后端按分区挑默认批次
      try { await load({ batch_id: undefined }); } catch (e) { toast(e.message, true); }
    });
    $("#filter-batch").addEventListener("change", async () => {
      filter.batch_id = Number($("#filter-batch").value) || null;
      if (filter.batch_id) filter.zone_id = null;  // 指定批次即取消分区筛选
      try { await load(); } catch (e) { toast(e.message, true); }
    });
    $("#btn-export").addEventListener("click", () => {
      const p = new URLSearchParams();
      if (state.selected_id) p.set("batch_id", state.selected_id);
      if (state.filter_zone_id) p.set("zone_id", state.filter_zone_id);
      window.location.href = "/api/heat/export?" + p.toString();
    });
  }

  // ------------------------------------------------------------ 启动

  $("#reading-form").elements.ts.value = nowLocalInput();
  bindForms();
  bindFilters();
  load().catch((e) => toast(e.message, true));
})();
