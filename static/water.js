/* 灌溉水质连续监测 —— 原生 JS */
(() => {
  "use strict";

  const STATUS_TEXT = { normal: "正常", observing: "观察", exceeded: "超标" };
  const CLASS_TEXT = { risk: "风险", review: "待复核", safe: "正常", insufficient: "证据不足" };
  const METRIC_ORDER = ["ph", "ec", "turbidity", "nitrate"];

  let state = null;
  let trace = null;
  const traceFilter = { date: "", zoneId: null };
  const filter = { source_id: null, date: "" };

  const $ = (sel, root = document) => root.querySelector(sel);

  // ------------------------------------------------------------ 工具

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 2800);
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

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function nowLocalInput() {
    const d = new Date();
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function fmtTs(ts) { return String(ts).replace("T", " "); }
  function fmtHm(ts) { return String(ts).slice(11, 16); }
  function fmtNum(v) { return (Math.round(v * 100) / 100).toString(); }

  function metricMeta(key) {
    return state.metrics.find((m) => m.key === key);
  }

  // ------------------------------------------------------------ 加载

  function stateUrl(extra = {}) {
    const p = new URLSearchParams();
    const sid = extra.source_id ?? filter.source_id ?? state?.selected_id;
    if (sid) p.set("source_id", sid);
    const day = extra.date !== undefined ? extra.date : filter.date;
    if (day) p.set("date", day);
    else p.set("limit", extra.limit ?? 50);
    return "/api/water/state?" + p.toString();
  }

  function traceUrl(extra = {}) {
    const p = new URLSearchParams();
    if (traceFilter.date) p.set("date", traceFilter.date);
    const zid = extra.zoneId !== undefined ? extra.zoneId : traceFilter.zoneId;
    if (zid) p.set("zone_id", zid);
    return "/api/water/trace?" + p.toString();
  }

  async function loadTrace() {
    trace = await api(traceUrl());
    // 保留当前日期的全量分区清单，供筛选下拉在单分区视图下切换
    try {
      const full = await api(traceUrl({ zoneId: null }));
      trace._all_zones = full.zones;
    } catch (e) { trace._all_zones = trace.zones; }
    renderTrace();
  }

  async function load() {
    state = await api(stateUrl());
    if (state.selected_id) filter.source_id = state.selected_id;
    if (!traceFilter.date && state.plan_date) traceFilter.date = state.plan_date;
    renderAll();
    try { await loadTrace(); } catch (e) { toast(e.message, true); }
  }

  function renderAll() {
    renderSources();
    renderSummary();
    renderFilterBar();
    renderRanges();
    renderCharts();
    renderTable();
    renderValveBindings();
    renderTraceControls();
    syncMetaForm();
  }

  // ------------------------------------------------------------ 水源列表 / 总览

  function renderSources() {
    const ul = $("#source-list");
    ul.innerHTML = "";
    if (!state.sources.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">暂无水源，请先添加</li>`;
      return;
    }
    state.sources.forEach((s) => {
      const li = document.createElement("li");
      li.className = "entity-row source-item" +
        (s.id === state.selected_id ? " selected" : "");
      li.innerHTML =
        `<span class="grow">
           <span class="name"><i class="dot ${s.status}"></i>${escapeHtml(s.name)}</span>
           <div class="meta"><span class="badge ${s.status}">${STATUS_TEXT[s.status]}</span>
             ${s.triggers.length ? "· " + escapeHtml(s.triggers.join("、")) : ""}</div>
           <div class="meta">${s.sample_count} 条采样${s.last_ts ? " · " + fmtTs(s.last_ts) : ""}</div>
         </span>`;
      li.addEventListener("click", () => selectSource(s.id));
      ul.appendChild(li);
    });
  }

  function renderSummary() {
    const box = $("#overall-box");
    const badge = $("#overall-badge");
    badge.className = "overall-badge " + state.overall_status;
    badge.textContent = state.sources.length ? STATUS_TEXT[state.overall_status] : "—";
    box.title = "所有水源当前状态取最差者";

    const ul = $("#source-summary");
    ul.innerHTML = "";
    state.sources.forEach((s) => {
      const li = document.createElement("li");
      li.style.cursor = "pointer";
      li.innerHTML =
        `<span class="dot ${s.status}"></span>
         <span class="src-name">${escapeHtml(s.name)}</span>
         <span class="badge ${s.status}">${STATUS_TEXT[s.status]}</span>
         <span class="src-meta">${s.triggers.length ? escapeHtml(s.triggers.join("、")) : "全部指标在区间内"}
           · ${s.sample_count} 条</span>`;
      li.addEventListener("click", () => selectSource(s.id));
      ul.appendChild(li);
    });
  }

  function renderFilterBar() {
    const sel = $("#filter-source");
    sel.innerHTML = state.sources.length
      ? state.sources.map((s) =>
          `<option value="${s.id}"${s.id === state.selected_id ? " selected" : ""}>` +
          `${escapeHtml(s.name)}（${STATUS_TEXT[s.status]}）</option>`).join("")
      : `<option value="">（无水源）</option>`;
    $("#filter-date").value = state.date || "";
  }

  function syncMetaForm() {
    const form = $("#source-meta-form");
    const s = state.sources.find((x) => x.id === state.selected_id);
    form.elements.id.value = s ? s.id : "";
    form.elements.note.value = s ? (s.note || "") : "";
    $("#btn-source-delete").disabled = !s;
    $("#sample-form").style.opacity = s ? "1" : ".55";
    $("#range-form").style.opacity = s ? "1" : ".55";
  }

  async function selectSource(id) {
    filter.source_id = id;
    filter.date = "";
    try { await load(); } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 区间表单

  function renderRanges() {
    const form = $("#range-form");
    METRIC_ORDER.forEach((k) => {
      const r = state.ranges[k];
      form.elements[`${k}_low`].value = r.low;
      form.elements[`${k}_high`].value = r.high;
    });
    const s = state.sources.find((x) => x.id === state.selected_id);
    $("#range-source-name").textContent = s ? `· ${s.name}` : "";
  }

  // ------------------------------------------------------------ 趋势图（内联 SVG）

  function renderCharts() {
    const wrap = $("#charts");
    const subtitle = $("#trend-subtitle");
    wrap.innerHTML = "";
    const samples = state.samples;
    if (!state.selected_id) {
      subtitle.textContent = "";
      wrap.innerHTML = `<div class="chart-empty">请先在左侧添加并选择水源</div>`;
      return;
    }
    if (!samples.length) {
      subtitle.textContent = "";
      wrap.innerHTML = `<div class="chart-empty">该水源${filter.date ? "在所选日期" : ""}暂无采样记录</div>`;
      return;
    }
    subtitle.textContent =
      (filter.date ? `${filter.date} · ` : `最近 ${samples.length} 条 · `) +
      "灰色色带为正常区间，点色为该指标当时状态（绿=正常 / 橙=观察 / 红=超标）";

    const W = 340, H = 110, PAD_L = 34, PAD_R = 10, PAD_T = 12, PAD_B = 18;
    const n = samples.length;
    const xAt = (i) => PAD_L + (n === 1 ? (W - PAD_L - PAD_R) / 2 :
      i * (W - PAD_L - PAD_R) / (n - 1));

    METRIC_ORDER.forEach((key) => {
      const meta = metricMeta(key);
      const vals = samples.map((s) => s[key]);
      let lo = Math.min(...vals, meta.low);
      let hi = Math.max(...vals, meta.high);
      if (lo === hi) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.12;
      lo -= pad; hi += pad;
      const yAt = (v) => PAD_T + (hi - v) * (H - PAD_T - PAD_B) / (hi - lo);

      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

      // 正常区间色带
      const band = document.createElementNS(svgNS, "rect");
      band.setAttribute("class", "range-band");
      band.setAttribute("x", PAD_L);
      band.setAttribute("width", W - PAD_L - PAD_R);
      band.setAttribute("y", yAt(meta.high));
      band.setAttribute("height", Math.max(0, yAt(meta.low) - yAt(meta.high)));
      svg.appendChild(band);

      // 折线
      const pts = samples.map((s, i) => `${xAt(i)},${yAt(s[key])}`).join(" ");
      const line = document.createElementNS(svgNS, "polyline");
      line.setAttribute("class", "trend-line");
      line.setAttribute("points", pts);
      svg.appendChild(line);

      // 数据点（按该指标当时状态着色）
      samples.forEach((s, i) => {
        const c = document.createElementNS(svgNS, "circle");
        c.setAttribute("class", "trend-pt pt-" + s.metric_states[key]);
        c.setAttribute("cx", xAt(i));
        c.setAttribute("cy", yAt(s[key]));
        c.setAttribute("r", n > 30 ? 2.4 : 3.4);
        c.appendChild(document.createElementNS(svgNS, "title")).textContent =
          `${fmtTs(s.ts)}\n${meta.label}：${fmtNum(s[key])} ${meta.unit}\n` +
          `状态：${STATUS_TEXT[s.metric_states[key]]}`;
        svg.appendChild(c);
      });

      // 坐标标注：区间上下限 + 首末时间
      [
        { v: meta.high, y: yAt(meta.high) },
        { v: meta.low, y: yAt(meta.low) },
      ].forEach(({ v, y }) => {
        const t = document.createElementNS(svgNS, "text");
        t.setAttribute("class", "chart-axis");
        t.setAttribute("x", 3);
        t.setAttribute("y", Math.min(H - 4, Math.max(9, y + 3)));
        t.textContent = fmtNum(v);
        svg.appendChild(t);
      });
      const t0 = document.createElementNS(svgNS, "text");
      t0.setAttribute("class", "chart-axis");
      t0.setAttribute("x", PAD_L);
      t0.setAttribute("y", H - 5);
      t0.textContent = samples[0]["ts"].slice(5, 16).replace("T", " ");
      svg.appendChild(t0);
      const t1 = document.createElementNS(svgNS, "text");
      t1.setAttribute("class", "chart-axis");
      t1.setAttribute("x", W - PAD_R);
      t1.setAttribute("y", H - 5);
      t1.setAttribute("text-anchor", "end");
      t1.textContent = samples[n - 1]["ts"].slice(5, 16).replace("T", " ");
      svg.appendChild(t1);

      const box = document.createElement("div");
      box.className = "chart-box";
      const h3 = document.createElement("h3");
      h3.innerHTML = `<span>${escapeHtml(meta.label)}</span>` +
        `<small>区间 ${fmtNum(meta.low)} ~ ${fmtNum(meta.high)} ${escapeHtml(meta.unit)}</small>`;
      box.append(h3, svg);
      wrap.appendChild(box);
    });
  }

  // ------------------------------------------------------------ 明细表

  function renderTable() {
    const tbody = $("#sample-rows");
    const subtitle = $("#detail-subtitle");
    tbody.innerHTML = "";
    const samples = [...state.samples].reverse(); // 最新在前
    subtitle.textContent = state.selected_id
      ? `共 ${samples.length} 条${filter.date ? "（" + filter.date + "）" : "（最近 50 条）"}`
      : "";
    if (!samples.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="8">暂无采样记录</td></tr>`;
      return;
    }
    samples.forEach((s) => {
      const tr = document.createElement("tr");
      const cells = METRIC_ORDER.map((k) => {
        const meta = metricMeta(k);
        const out = s[k] < meta.low || s[k] > meta.high;
        return `<td class="${out ? "violation" : ""}">${fmtNum(s[k])}</td>`;
      }).join("");
      tr.innerHTML =
        `<td>${fmtTs(s.ts)}</td>
         <td><span class="badge ${s.status}">${STATUS_TEXT[s.status]}</span></td>
         <td class="triggers">${s.triggers.length ? escapeHtml(s.triggers.join("、")) : "—"}</td>
         ${cells}
         <td><button class="del-btn" title="删除该采样（删除后重算状态）">🗑</button></td>`;
      tr.querySelector(".del-btn").addEventListener("click", () => removeSample(s));
      tbody.appendChild(tr);
    });
  }

  async function removeSample(s) {
    if (!confirm(`删除 ${fmtTs(s.ts)} 的采样？删除后将按时间重算该水源状态。`)) return;
    try {
      await api(`/api/water/samples/${s.id}`, { method: "DELETE" });
      await load();
      toast("采样已删除，状态已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 阀门水源绑定

  function renderValveBindings() {
    const ul = $("#valve-bind-list");
    ul.innerHTML = "";
    if (!state.valves.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">暂无阀门，请先在轮灌编排台添加</li>`;
      return;
    }
    state.valves.forEach((v) => {
      const li = document.createElement("li");
      li.className = "entity-row valve-bind-row";
      const opts =
        `<option value="">（未绑定）</option>` +
        state.sources.map((s) =>
          `<option value="${s.id}"${s.id === v.source_id ? " selected" : ""}>` +
          `${escapeHtml(s.name)}</option>`).join("");
      li.innerHTML =
        `<span class="grow">
           <span class="name">${escapeHtml(v.name)}</span>
           <div class="meta">${fmtNum(v.flow_lmin)} L/min${v.source_name ? " · 当前：" + escapeHtml(v.source_name) : " · 未绑定水源"}</div>
         </span>
         <select class="bind-select" data-valve="${v.id}" title="绑定水源">${opts}</select>`;
      li.querySelector(".bind-select").addEventListener("change", async (e) => {
        const source_id = e.target.value ? Number(e.target.value) : null;
        try {
          await api(`/api/valves/${v.id}/source`, {
            method: "PUT", body: { source_id },
          });
          state = await api(stateUrl());
          renderAll();
          await loadTrace();
          toast(source_id ? `已绑定水源，回溯已重新核算` : "已解除绑定，回溯标记为证据不足");
        } catch (err) { toast(err.message, true); }
      });
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 用水回溯

  function renderTraceControls() {
    $("#trace-date").value = traceFilter.date || state.plan_date || "";
    const sel = $("#trace-zone");
    // 分区选项始终取当前日期的全量回溯结果，便于在筛选单一分区后切回其他分区
    const rows = trace?._all_zones || trace?.zones || [];
    sel.innerHTML = `<option value="">全部分区</option>` +
      rows.map((r) =>
        `<option value="${r.zone_id}"${r.zone_id === traceFilter.zoneId ? " selected" : ""}>` +
        `${escapeHtml(r.zone_name)}</option>`).join("");
  }

  function sourceLabel(o) {
    if (o.source_kind === "sample")
      return `当日采样 ${fmtTs(o.source_sample_ts)}`;
    if (o.source_kind === "carry")
      return `历史采样 ${fmtTs(o.source_sample_ts)}`;
    if (o.source_kind === "unbound") return "阀门未绑定水源";
    return "无历史样本";
  }

  function renderTrace() {
    const box = $("#trace-summary");
    const tbody = $("#trace-rows");
    const hint = $("#trace-hint");
    box.innerHTML = "";
    tbody.innerHTML = "";
    if (!trace) return;
    $("#trace-date").value = traceFilter.date || trace.date || "";

    const s = trace.summary;
    const chips = [
      ["risk", `风险水量 ${fmtNum(s.risk_l)} L（${s.risk_zones.length} 区）`],
      ["review", `待复核 ${fmtNum(s.review_l)} L（${s.review_zones.length} 区）`],
      ["insufficient", `证据不足 ${fmtNum(s.insufficient_l)} L（${s.insufficient_zones.length} 区）`],
      ["safe", `正常用水 ${fmtNum(s.safe_l)} L`],
    ];
    chips.forEach(([k, text]) => {
      const span = document.createElement("span");
      span.className = "trace-chip chip-" + k;
      span.textContent = text;
      box.appendChild(span);
    });
    if (trace.unbound_valves.length) {
      const span = document.createElement("span");
      span.className = "trace-chip chip-insufficient";
      span.textContent = "未绑定水源阀门：" +
        trace.unbound_valves.map((v) => v.name).join("、");
      box.appendChild(span);
    }

    if (!trace.zones.length) {
      tbody.innerHTML =
        `<tr class="empty-row"><td colspan="8">该日期没有已编排的 15 分钟灌溉时段` +
        `（回溯读取现有计划，不改动原计划）</td></tr>`;
      hint.textContent = "提示：可在轮灌编排台为该计划日期生成或调整计划后再核算。";
      return;
    }
    hint.textContent = "采样时刻起采用新状态；当日首条采样前沿用最近历史采样，无历史样本标为证据不足。" +
      "超标 / 观察区间分别计为风险 / 待复核，水量 = 阀门流量 × 重叠分钟。";

    trace.zones.forEach((r) => {
      const tr = document.createElement("tr");
      tr.className = "trace-zone-row klass-" + r.klass;
      const srcText = r.source_name
        ? `${escapeHtml(r.valve_name || "—")} / ${escapeHtml(r.source_name)}`
        : `${escapeHtml(r.valve_name || "—")} / <em>未绑定</em>`;
      tr.innerHTML =
        `<td>${escapeHtml(r.zone_name)}</td>
         <td>${fmtHm(r.start)}–${fmtHm(r.end)}（${r.planned_minutes} 分钟）</td>
         <td>${srcText}</td>
         <td><span class="badge trace-badge ${r.klass}">${CLASS_TEXT[r.klass]}</span></td>
         <td class="num">${r.risk_l ? fmtNum(r.risk_l) : "—"}</td>
         <td class="num">${r.review_l ? fmtNum(r.review_l) : "—"}</td>
         <td class="triggers">${r.triggers.length ? escapeHtml(r.triggers.join("、")) : "—"}</td>
         <td class="trace-src">${r.overlaps.map((o) =>
           `${fmtHm(o.start)}–${fmtHm(o.end)}：${escapeHtml(sourceLabel(o))}`)
           .join("<br>")}</td>`;
      tbody.appendChild(tr);
    });

    renderTraceControls();
  }

  // ------------------------------------------------------------ 表单

  function bindForms() {
    $("#source-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        const created = await api("/api/water/sources", {
          method: "POST",
          body: { name: f.elements.name.value.trim() },
        });
        f.reset();
        filter.source_id = created.selected_id;
        filter.date = "";
        await load();
        toast("水源已添加，区间为默认值，可在下方调整");
      } catch (err) { toast(err.message, true); }
    });

    $("#source-meta-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const id = f.elements.id.value;
      if (!id) return;
      const cur = state.sources.find((x) => x.id === Number(id));
      try {
        await api(`/api/water/sources/${id}`, {
          method: "PUT",
          body: { name: cur.name, note: f.elements.note.value.trim() },
        });
        await load();
        toast("备注已保存");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-source-delete").addEventListener("click", async () => {
      const s = state.sources.find((x) => x.id === state.selected_id);
      if (!s) return;
      if (!confirm(`删除水源「${s.name}」？其全部采样与区间配置将一并删除。`)) return;
      try {
        await api(`/api/water/sources/${s.id}`, { method: "DELETE" });
        filter.source_id = null;
        await load();
        toast("水源已删除");
      } catch (err) { toast(err.message, true); }
    });

    $("#sample-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.selected_id) { toast("请先选择水源", true); return; }
      const f = e.target;
      const body = {
        ts: f.elements.ts.value,
        ph: Number(f.elements.ph.value),
        ec: Number(f.elements.ec.value),
        turbidity: Number(f.elements.turbidity.value),
        nitrate: Number(f.elements.nitrate.value),
      };
      try {
        await api(`/api/water/sources/${state.selected_id}/samples`, {
          method: "POST", body,
        });
        f.reset();
        f.elements.ts.value = nowLocalInput();
        await load();
        const s = state.sources[state.sources.length - 1];
        toast(s ? `采样已提交，当前状态：${STATUS_TEXT[s.status]}` : "采样已提交");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-now").addEventListener("click", () => {
      $("#sample-form").elements.ts.value = nowLocalInput();
    });

    $("#range-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.selected_id) return;
      const f = e.target;
      const ranges = {};
      METRIC_ORDER.forEach((k) => {
        ranges[k] = [Number(f.elements[`${k}_low`].value),
                     Number(f.elements[`${k}_high`].value)];
      });
      try {
        await api(`/api/water/sources/${state.selected_id}/ranges`, {
          method: "PUT", body: { ranges },
        });
        await load();
        toast("区间已保存，全部历史采样状态已重算");
      } catch (err) { toast(err.message, true); }
    });
  }

  // ------------------------------------------------------------ 筛选 / 导出

  function bindFilters() {
    $("#btn-filter-apply").addEventListener("click", async () => {
      if (!state.selected_id) { toast("请先选择水源", true); return; }
      filter.source_id = Number($("#filter-source").value);
      filter.date = $("#filter-date").value;
      try { await load(); } catch (e) { toast(e.message, true); }
    });
    $("#filter-source").addEventListener("change", () => {
      filter.source_id = Number($("#filter-source").value) || null;
      filter.date = "";
      load().catch((e) => toast(e.message, true));
    });
    $("#btn-filter-clear").addEventListener("click", () => {
      filter.date = "";
      load().catch((e) => toast(e.message, true));
    });
    $("#btn-export").addEventListener("click", () => {
      const p = new URLSearchParams();
      if (state.selected_id) p.set("source_id", state.selected_id);
      if (filter.date) p.set("date", filter.date);
      window.location.href = "/api/water/export?" + p.toString();
    });

    $("#btn-trace-apply").addEventListener("click", async () => {
      traceFilter.date = $("#trace-date").value || "";
      traceFilter.zoneId = Number($("#trace-zone").value) || null;
      try { await loadTrace(); } catch (e) { toast(e.message, true); }
    });
    $("#trace-zone").addEventListener("change", () => {
      traceFilter.zoneId = Number($("#trace-zone").value) || null;
      loadTrace().catch((e) => toast(e.message, true));
    });
    $("#btn-trace-clear").addEventListener("click", () => {
      traceFilter.zoneId = null;
      loadTrace().catch((e) => toast(e.message, true));
    });
    $("#btn-trace-export").addEventListener("click", () => {
      const p = new URLSearchParams();
      if (traceFilter.date) p.set("date", traceFilter.date);
      if (traceFilter.zoneId) p.set("zone_id", traceFilter.zoneId);
      window.location.href = "/api/water/trace/export?" + p.toString();
    });
  }

  // ------------------------------------------------------------ 启动

  $("#sample-form").elements.ts.value = nowLocalInput();
  bindForms();
  bindFilters();
  load().catch((e) => toast(e.message, true));
})();
