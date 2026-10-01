/* 滴灌均匀度巡检 —— 原生 JS */
(() => {
  "use strict";

  let state = null;
  const filter = { round_id: null, valve_id: null, date: "" };
  const detail = { drip_zone_id: "", position: "", low_only: false };
  let collapsed = new Set();

  const $ = (sel, root = document) => root.querySelector(sel);

  const POS_OPTS = [
    ["head", "首段"], ["middle", "中段"], ["tail", "尾段"],
  ];
  const POS_LABEL = { head: "首段", middle: "中段", tail: "尾段" };
  const DEFAULT_CFG = {
    base_inlet_pressure: 0.10, design_flow: 2.0, du_min: 0.80,
    tail_drop_limit: 0.10, low_flow_ratio: 0.70, inlet_pressure: 0.10,
  };

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
    const rid = extra.round_id ?? filter.round_id;
    const vid = extra.valve_id ?? filter.valve_id;
    const day = extra.date ?? filter.date;
    if (rid) p.set("round_id", rid);
    if (vid) p.set("valve_id", vid);
    if (day) p.set("date", day);
    const qs = p.toString();
    return "/api/drip/state" + (qs ? "?" + qs : "");
  }

  async function load(extra) {
    state = await api(stateUrl(extra));
    if (state.selected_id) filter.round_id = state.selected_id;
    renderAll();
  }

  function renderAll() {
    renderSelectors();
    renderRoundList();
    renderRoundForm();
    renderOverview();
    renderMaintenance();
    renderZoneCards();
    renderSampleDetail();
  }

  const sel = () => state.selected;

  // ------------------------------------------------------------ 选择器

  function renderSelectors() {
    const fv = $("#filter-valve"), fr = $("#filter-round"), rv = $("#round-valve");
    fv.innerHTML = `<option value="">全部阀门</option>` +
      state.valves.map((v) => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join("");
    fv.value = state.filter_valve_id || "";

    fr.innerHTML = state.rounds.length
      ? state.rounds.map((r) =>
          `<option value="${r.id}"${r.id === state.selected_id ? " selected" : ""}>` +
          `${fmtTs(r.inspected).slice(0, 16)} ${escapeHtml(r.name)}</option>`).join("")
      : `<option value="">（无巡检轮次）</option>`;
    $("#filter-date").value = state.filter_date || "";

    rv.innerHTML = state.valves.length
      ? state.valves.map((v) => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join("")
      : `<option value="">（请先建立阀门）</option>`;

    $("#sample-card").style.opacity = state.selected_id ? "1" : ".55";
  }

  // ------------------------------------------------------------ 轮次列表

  function renderRoundList() {
    const ul = $("#round-list");
    ul.innerHTML = "";
    if (!state.rounds.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">` +
        (state.valves.length ? "没有符合筛选的巡检轮次，请在下方新建"
                             : "请先在轮灌编排台建立阀门与分区") + `</li>`;
      return;
    }
    state.rounds.forEach((r) => {
      const li = document.createElement("li");
      li.className = "entity-row round-item" + (r.id === state.selected_id ? " selected" : "");
      li.innerHTML =
        `<span class="line1">
           <span class="grow name">${escapeHtml(r.name)}</span>
           <span class="zone-chip">${escapeHtml(r.valve_name)}</span>
         </span>
         <span class="meta">
           <span><i class="verdict-dot ${r.verdict}"></i>${r.verdict_label}</span>
           <span>🕒 ${fmtTs(r.inspected).slice(0, 16)}</span>
         </span>
         <span class="meta">
           ${r.evaluated_count}/${r.zone_count} 区已评 · DU 合格 ${r.du_pass_count} 区
           · 样本 ${r.sample_count} 个
         </span>`;
      li.addEventListener("click", () => selectRound(r.id));
      ul.appendChild(li);
    });
  }

  async function selectRound(id) {
    filter.round_id = id;
    filter.valve_id = null;
    filter.date = "";
    try { await load({ valve_id: undefined, date: undefined }); }
    catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 轮次表单（新建 / 编辑）

  function renderRoundForm() {
    const f = $("#round-form");
    const s = sel();
    $("#btn-round-delete").disabled = !s;
    const cfgs = $("#zone-configs");

    if (!s) {
      f.elements.id.value = "";
      f.elements.name.value = "";
      f.elements.note.value = "";
      f.elements.inspected.value = nowLocalInput();
      $("#round-submit").textContent = "新建巡检";
      $("#round-cancel").classList.add("hidden");
      renderZoneConfigs(null);
      return;
    }
    f.elements.id.value = s.id;
    f.elements.name.value = s.name;
    f.elements.note.value = s.note || "";
    f.elements.inspected.value = (s.inspected || "").slice(0, 16);
    f.elements.valve_id.value = s.valve_id;
    $("#round-submit").textContent = "保存轮次并重算";
    $("#round-cancel").classList.remove("hidden");
    renderZoneConfigs(s);
  }

  function cfgInput(z, key, label, unit, step = "0.01") {
    const val = z ? z[key] : DEFAULT_CFG[key];
    return `<label>${label}${unit ? ` <small>${unit}</small>` : ""}
      <input type="number" data-cfg="${key}" step="${step}" min="0" value="${fmtNum(val, 3)}"></label>`;
  }

  function renderZoneConfigs(s) {
    const cfgs = $("#zone-configs");
    const valveId = s ? s.valve_id : Number($("#round-valve").value) || null;
    const zones = state.zones.filter((z) => z.valve_id === valveId);
    const existing = {};
    if (s) for (const z of s.result.zones) existing[z.zone_id] = z;

    if (!valveId || !zones.length) {
      cfgs.innerHTML = `<p class="form-hint">该阀门下暂无可巡检分区，请先在轮灌编排台把分区绑定到阀门。</p>`;
      return;
    }
    cfgs.innerHTML = zones.map((z) => {
      const e = existing[z.zone_id];
      const inletVal = e ? (e.inlet_pressure ?? "") : DEFAULT_CFG.inlet_pressure;
      const g = (k, d = 3) => e ? fmtNum(e[k], d) : fmtNum(DEFAULT_CFG[k], d);
      return `<div class="zcfg" data-zone-id="${z.id}">
        <div class="zcfg-title">${escapeHtml(z.name)} <span class="valve-tag">${escapeHtml(z.valve_name)}</span></div>
        <div class="zcfg-grid">
          <label>实测入口压力 <small>MPa（可空）</small>
            <input type="number" data-cfg="inlet_pressure" step="0.01" min="0" value="${inletVal === "" ? "" : g("inlet_pressure")}"></label>
          <label>基准入口压力 <small>MPa</small>
            <input type="number" data-cfg="base_inlet_pressure" step="0.01" min="0.01" value="${g("base_inlet_pressure")}"></label>
          <label>设计滴头流量 <small>L/h</small>
            <input type="number" data-cfg="design_flow" step="0.01" min="0.01" value="${g("design_flow")}"></label>
          <label>DU 下限 <small>0~1</small>
            <input type="number" data-cfg="du_min" step="0.01" min="0.01" max="1" value="${g("du_min")}"></label>
          <label>尾首降幅阈值 <small>0~1</small>
            <input type="number" data-cfg="tail_drop_limit" step="0.01" min="0" max="0.99" value="${g("tail_drop_limit")}"></label>
          <label>低流量比例 <small>0~1</small>
            <input type="number" data-cfg="low_flow_ratio" step="0.01" min="0.01" max="1" value="${g("low_flow_ratio")}"></label>
        </div>
      </div>`;
    }).join("");
  }

  function collectZoneConfigs() {
    const blocks = [...document.querySelectorAll("#zone-configs .zcfg")];
    if (!blocks.length) throw new Error("该阀门下没有可巡检分区");
    return blocks.map((b) => {
      const out = { zone_id: Number(b.dataset.zoneId) };
      b.querySelectorAll("[data-cfg]").forEach((inp) => {
        out[inp.dataset.cfg] = inp.value === "" ? null : Number(inp.value);
      });
      return out;
    });
  }

  // ------------------------------------------------------------ 总览

  function renderOverview() {
    const s = sel();
    const title = $("#drip-title"), badge = $("#drip-badge");
    const meta = $("#drip-meta"), stats = $("#drip-stats"), rule = $("#rule-box");
    stats.innerHTML = ""; rule.innerHTML = ""; badge.className = "case-badge";
    if (!s) {
      title.textContent = "未选择巡检轮次";
      badge.textContent = "";
      meta.textContent = "在左侧新建巡检或从历史列表中选择轮次。";
      return;
    }
    const r = s.result;
    title.textContent = `${s.name}（${s.valve_name}）`;
    badge.classList.add(r.shared_triggered ? "shared" : "ok");
    badge.textContent = r.verdict_label;
    meta.innerHTML =
      `巡检时间 <b>${fmtTs(s.inspected)}</b>` +
      `${s.note ? ` · ${escapeHtml(s.note)}` : ""}` +
      ` · 分区 ${r.zone_count} 个（已评 ${r.evaluated_count}）` +
      ` · DU 合格 ${r.du_pass_count} 区 · 共 ${sumSamples(r)} 个滴头样本`;

    const items = [
      ["供压偏低且DU合格分区", `${r.shared_hit_count}/${r.zone_count}`, "个"],
      ["DU 合格分区", `${r.du_pass_count}/${r.evaluated_count}`, "个"],
      ["维护事项", r.maintenance.length, "项"],
    ];
    items.forEach(([label, val, unit]) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="stat-label">${label}</span>` +
        `<span class="stat-val">${escapeHtml(String(val))} <span class="stat-unit">${unit}</span></span>`;
      stats.appendChild(li);
    });

    rule.className = "rule-box " + (r.shared_triggered ? "shared" : "normal");
    rule.innerHTML = (r.shared_triggered
      ? `🚨 <strong>整轮判定：共享供压异常</strong>`
      : `✅ <strong>整轮判定：正常</strong>`) +
      `<div class="fc-detail">${escapeHtml(r.rule)}</div>`;
  }

  function sumSamples(r) {
    return r.zones.reduce((a, z) => a + z.sample_count, 0);
  }

  // ------------------------------------------------------------ 维护清单

  const M_ICON = {
    shared_pressure: "🚨", pipe_loss: "⚠️", clog: "🔧",
    pressure: "📉", pressure_missing: "❓", du_fail: "❓", insufficient: "📝",
  };

  function renderMaintenance() {
    const list = $("#maintain-list"), sub = $("#maintain-subtitle");
    list.innerHTML = "";
    const s = sel();
    if (!s) { sub.textContent = ""; return; }
    const items = s.result.maintenance;
    sub.textContent = `共 ${items.length} 项（按优先级排序）`;
    if (!items.length) {
      list.innerHTML = `<li class="maintain-empty">本轮无维护事项，各分区 DU 与供压均正常。</li>`;
      return;
    }
    items.forEach((m) => {
      const li = document.createElement("li");
      li.className = `maintain-item ${m.priority}`;
      li.innerHTML =
        `<span class="m-icon">${M_ICON[m.type] || "🛠"}</span>` +
        `<span class="m-body">
           <span class="m-action">${escapeHtml(m.action)}</span>
           <span class="m-basis">${escapeHtml(m.basis)}</span>
         </span>` +
        (m.zone_name ? `<span class="m-zone">${escapeHtml(m.zone_name)}</span>` : "");
      list.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 分区结果卡

  function metric(label, val, unit, cls = "") {
    return `<span class="zc-metric">${label}<b class="${cls}">${val}` +
           (unit ? ` <span class="stat-unit">${unit}</span>` : "") + `</b></span>`;
  }

  function renderZoneCards() {
    const wrap = $("#zone-cards");
    wrap.innerHTML = "";
    const s = sel();
    // 录样分区下拉
    const sz = $("#sample-zone"), dz = $("#detail-zone");
    if (!s) {
      sz.innerHTML = `<option value="">（先选择轮次）</option>`;
      dz.innerHTML = `<option value="">（先选择轮次）</option>`;
      wrap.innerHTML = `<div class="chart-empty">请先选择巡检轮次</div>`;
      return;
    }
    const opts = s.result.zones.map((z) =>
      `<option value="${z.drip_zone_id}">${escapeHtml(z.zone_name)}（${z.sample_count} 个样本）</option>`).join("");
    sz.innerHTML = opts;
    if (![...sz.options].some((o) => o.value === String(detail.drip_zone_id))) {
      detail.drip_zone_id = sz.options[0]?.value || "";
    }
    sz.value = detail.drip_zone_id;
    dz.innerHTML = s.result.zones.map((z) =>
      `<option value="${z.drip_zone_id}">${escapeHtml(z.zone_name)}</option>`).join("");
    if (![...dz.options].some((o) => o.value === String(detail.drip_zone_id))) {
      detail.drip_zone_id = dz.options[0]?.value || "";
    }
    dz.value = detail.drip_zone_id;

    s.result.zones.forEach((z) => {
      const card = document.createElement("div");
      card.className = `zone-card ${z.verdict}`;
      const isCol = collapsed.has(z.drip_zone_id);
      const duTxt = z.evaluated
        ? `${fmtNum(z.du, 4)} <small>/ ${fmtNum(z.du_min, 2)}</small>` : "—";
      const dropTxt = z.tail_head_drop === null ? "—" : `${z.tail_head_drop_pct}%`;
      const pTxt = z.inlet_pressure === null ? "未测"
        : `${fmtNum(z.inlet_pressure)}/${fmtNum(z.base_inlet_pressure)}`;
      card.innerHTML =
        `<div class="zc-head" data-id="${z.drip_zone_id}">
           <span class="zc-caret">${isCol ? "▶" : "▼"}</span>
           <span class="zc-name">${escapeHtml(z.zone_name)}</span>
           <span class="zc-badge ${z.verdict}">${z.verdict_label}</span>
           <span class="zc-summary">
             <span>DU <b class="${z.evaluated ? (z.du_pass ? "pass" : "fail") : ""}">${duTxt}</b></span>
             <span>入口/基准 <b class="${z.pressure_ok ? "pass" : "fail"}">${pTxt}</b> MPa</span>
             <span>尾首降幅 <b class="${z.tail_drop_pass ? "fail" : ""}">${dropTxt}</b></span>
             <span>样本 <b>${z.sample_count}</b></span>
           </span>
         </div>
         <div class="zc-body ${isCol ? "collapsed" : ""}">
           <div class="zc-metrics">
             ${metric("低四分位均值", fmtNum(z.low_quarter_mean), "L/h")}
             ${metric("全样本均值", fmtNum(z.mean_flow), "L/h")}
             ${metric("低四分位样本数", z.low_quarter_count ?? "—", `个（共 ${z.sample_count}）`)}
             ${metric("首段中位数", fmtNum(z.head_median), "L/h")}
             ${metric("中段中位数", fmtNum(z.middle_median), "L/h")}
             ${metric("尾段中位数", fmtNum(z.tail_median), "L/h")}
             ${metric("堵塞阈值", fmtNum(z.clog_threshold), "L/h")}
           </div>
           <ul class="zc-reasons">${z.reasons.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>
           ${z.clog_points.length && z.verdict !== "pipe_loss"
             ? `<div class="zc-clogs">🔧 疑似滴头堵塞采样点：` +
               z.clog_points.map((p) =>
                 `<span class="clog-chip">${p.position_label}${escapeHtml(p.label)} · ${fmtNum(p.flow)} L/h</span>`).join("") +
               `</div>` : ""}
           <details class="zc-edit">
             <summary>修改本分区参数 / 入口压力（保存后整轮重算）</summary>
             <div class="zcfg">
               <div class="zcfg-grid">
                 ${cfgEditInput(z, "inlet_pressure", "实测入口压力", "MPa（可空）", true)}
                 ${cfgEditInput(z, "base_inlet_pressure", "基准入口压力", "MPa")}
                 ${cfgEditInput(z, "design_flow", "设计滴头流量", "L/h")}
                 ${cfgEditInput(z, "du_min", "DU 下限", "0~1")}
                 ${cfgEditInput(z, "tail_drop_limit", "尾首降幅阈值", "0~1")}
                 ${cfgEditInput(z, "low_flow_ratio", "低流量比例", "0~1")}
               </div>
               <div class="form-actions">
                 <button class="btn small primary" type="button" data-save-zcfg="${z.drip_zone_id}">保存参数并重算</button>
               </div>
             </div>
           </details>
         </div>`;
      card.querySelector(".zc-head").addEventListener("click", () => {
        if (collapsed.has(z.drip_zone_id)) collapsed.delete(z.drip_zone_id);
        else collapsed.add(z.drip_zone_id);
        renderZoneCards();
      });
      card.querySelector(`[data-save-zcfg]`).addEventListener("click", (e) => {
        e.preventDefault();
        void saveZoneConfig(z.drip_zone_id, card);
      });
      wrap.appendChild(card);
    });
  }

  function cfgEditInput(z, key, label, unit, allowEmpty = false) {
    const v = z[key];
    return `<label>${label} <small>${unit}</small>
      <input type="number" data-edit-cfg="${key}" step="0.01"
        ${allowEmpty ? "" : "min=0.01"} value="${v === null || v === undefined ? "" : fmtNum(v, 3)}"></label>`;
  }

  async function saveZoneConfig(dzid, cardEl) {
    const body = {};
    cardEl.querySelectorAll("[data-edit-cfg]").forEach((inp) => {
      body[inp.dataset.edit_cfg] = inp.value === "" ? null : Number(inp.value);
    });
    try {
      await api(`/api/drip/zones/${dzid}`, { method: "PUT", body });
      await load();
      toast("分区参数已保存，整轮已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 样本明细表

  function renderSampleDetail() {
    const tbody = $("#sample-rows"), sub = $("#sample-subtitle");
    tbody.innerHTML = "";
    const s = sel();
    if (!s) { sub.textContent = ""; return; }
    const z = s.result.zones.find((x) => x.drip_zone_id === Number(detail.drip_zone_id));
    if (!z) {
      sub.textContent = "";
      tbody.innerHTML = `<tr class="empty-row"><td colspan="7">请选择分区</td></tr>`;
      return;
    }
    $("#detail-zone").value = detail.drip_zone_id;
    $("#detail-pos").value = detail.position;
    $("#detail-low-only").checked = detail.low_only;

    const POS_ORDER = { head: 0, middle: 1, tail: 2 };
    let rows = z.samples || [];
    sub.textContent =
      `${z.zone_name} · ${rows.length} 个样本` +
      `（堵塞阈值 ${fmtNum(z.clog_threshold)} L/h）`;
    let shown = rows
      .filter((r) => !detail.position || r.position === detail.position)
      .filter((r) => !detail.low_only || r.flow < z.clog_threshold)
      .sort((a, b) => POS_ORDER[a.position] - POS_ORDER[b.position]
        || String(a.label).localeCompare(String(b.label), "zh"));
    if (!shown.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="7">` +
        (rows.length ? "当前筛选下无样本" : "暂无样本，请在左侧录入（每区至少 8 个）") + `</td></tr>`;
      return;
    }
    shown.forEach((r) => {
      const low = r.flow < z.clog_threshold;
      const tr = document.createElement("tr");
      if (low) tr.classList.add("clog-row");
      tr.innerHTML =
        `<td>${escapeHtml(z.zone_name)}</td>` +
        `<td class="pos-${r.position}">${POS_LABEL[r.position]}</td>` +
        `<td>${escapeHtml(r.label)}</td>` +
        `<td class="${low ? "low-flow" : ""}">${fmtNum(r.flow, 2)}</td>` +
        `<td>${fmtNum(z.design_flow, 2)}</td>` +
        `<td>${fmtNum(z.clog_threshold, 2)}</td>` +
        `<td><button class="del-btn" title="删除该样本（删除后整轮重算）">🗑</button></td>`;
      tr.querySelector(".del-btn").addEventListener("click", () => removeSample(r));
      tbody.appendChild(tr);
    });
  }

  function escapeHtmlStr(s) { return escapeHtml(s); }

  async function removeSample(r) {
    if (!confirm(`删除 ${POS_LABEL[r.position]}${r.label} 号滴头样本（${fmtNum(r.flow)} L/h）？删除后整轮重算。`)) return;
    try {
      await api(`/api/drip/samples/${r.id}`, { method: "DELETE" });
      await load();
      toast("样本已删除，整轮已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 表单提交

  function bindForms() {
    // 新建表单中切换阀门 -> 重建分区参数块
    $("#round-valve").addEventListener("change", () => {
      if (!sel()) renderZoneConfigs(null);
    });

    $("#round-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      let zones;
      try {
        zones = collectZoneConfigs();
      } catch (err) { toast(err.message, true); return; }
      const body = {
        name: f.elements.name.value.trim(),
        valve_id: Number(f.elements.valve_id.value),
        inspected: f.elements.inspected.value,
        note: f.elements.note.value.trim(),
        zones,
      };
      const id = f.elements.id.value;
      try {
        if (id) {
          await api(`/api/drip/rounds/${id}`, { method: "PUT", body });
          toast("巡检轮次已保存，整轮已重算");
        } else {
          const r = await api("/api/drip/rounds", { method: "POST", body });
          filter.round_id = r.selected_id;
          toast("巡检轮次已创建");
        }
        await load();
      } catch (err) { toast(err.message, true); }
    });

    $("#round-cancel").addEventListener("click", () => {
      filter.round_id = null;
      collapsed.clear();
      void load({ round_id: undefined }).then(() => {
        $("#round-form").reset();
        $("#round-form").elements.id.value = "";
        $("#round-form").elements.inspected.value = nowLocalInput();
        $("#round-submit").textContent = "新建巡检";
        $("#round-cancel").classList.add("hidden");
        renderZoneConfigs(null);
      }).catch((e) => toast(e.message, true));
    });

    $("#btn-round-delete").addEventListener("click", async () => {
      const s = sel();
      if (!s) return;
      if (!confirm(`删除巡检轮次「${s.name}」？其全部分区参数与滴头样本将一并删除。`)) return;
      try {
        await api(`/api/drip/rounds/${s.id}`, { method: "DELETE" });
        filter.round_id = null;
        collapsed.clear();
        await load({ round_id: undefined });
        toast("巡检轮次已删除");
      } catch (err) { toast(err.message, true); }
    });

    $("#sample-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.selected_id) { toast("请先选择巡检轮次", true); return; }
      const f = e.target;
      const dzid = Number(f.elements.drip_zone_id.value);
      const body = {
        position: f.elements.position.value,
        label: f.elements.label.value.trim(),
        flow: Number(f.elements.flow.value),
      };
      if (!body.label) { toast("请填写滴头编号", true); return; }
      try {
        await api(`/api/drip/zones/${dzid}/samples`, { method: "POST", body });
        f.elements.label.value = "";
        f.elements.flow.value = "";
        detail.drip_zone_id = String(dzid);
        await load();
        toast("样本已录入，整轮已重算");
      } catch (err) { toast(err.message, true); }
    });
  }

  // ------------------------------------------------------------ 筛选 / 导出

  function bindFilters() {
    $("#filter-valve").addEventListener("change", async () => {
      filter.valve_id = Number($("#filter-valve").value) || null;
      filter.round_id = null;
      try { await load({ round_id: undefined }); } catch (e) { toast(e.message, true); }
    });
    $("#filter-date").addEventListener("change", async () => {
      filter.date = $("#filter-date").value;
      filter.round_id = null;
      try { await load({ round_id: undefined }); } catch (e) { toast(e.message, true); }
    });
    $("#btn-filter-clear").addEventListener("click", async () => {
      filter.valve_id = null; filter.date = ""; filter.round_id = null;
      try { await load({ round_id: undefined, valve_id: undefined, date: undefined }); }
      catch (e) { toast(e.message, true); }
    });
    $("#filter-round").addEventListener("change", async () => {
      const id = Number($("#filter-round").value) || null;
      if (!id) return;
      filter.round_id = id; filter.valve_id = null; filter.date = "";
      collapsed.clear();
      try { await load({ valve_id: undefined, date: undefined }); }
      catch (e) { toast(e.message, true); }
    });
    $("#detail-zone").addEventListener("change", () => {
      detail.drip_zone_id = $("#detail-zone").value;
      renderSampleDetail();
    });
    $("#detail-pos").addEventListener("change", () => {
      detail.position = $("#detail-pos").value;
      renderSampleDetail();
    });
    $("#detail-low-only").addEventListener("change", () => {
      detail.low_only = $("#detail-low-only").checked;
      renderSampleDetail();
    });
    $("#sample-zone").addEventListener("change", () => {
      detail.drip_zone_id = $("#sample-zone").value;
    });
    $("#btn-export").addEventListener("click", () => {
      const p = new URLSearchParams();
      if (state.selected_id && !state.filter_valve_id && !state.filter_date) {
        p.set("round_id", state.selected_id);
      } else {
        if (state.filter_valve_id) p.set("valve_id", state.filter_valve_id);
        if (state.filter_date) p.set("date", state.filter_date);
      }
      window.location.href = "/api/drip/export?" + p.toString();
    });
  }

  // ------------------------------------------------------------ 启动

  $("#round-form").elements.inspected.value = nowLocalInput();
  bindForms();
  bindFilters();
  load().catch((e) => toast(e.message, true));
})();
