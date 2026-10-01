/* 执行核验视图：批次选择、事件导入（幂等 / 整批拒绝）、台账渲染、删除与导出。
 * 挂在全局 window.VerifyView 上，由 app.js 切换视图时调用。 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let state = null;          // GET /api/verify/batches 返回
  let currentId = null;      // 当前展示的批次 id

  function toast(msg, isError = false) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 4000);
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
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败 (${res.status})`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const num = (x) => {
    const n = Number(x || 0);
    return n === Math.round(n) ? String(n) : n.toFixed(2);
  };
  const hm = (ts) => (ts || "").slice(11, 16);
  const STATUS_CLASS = {
    ok: "st-ok", deficit: "st-deficit", over: "st-over",
    insufficient: "st-amb", unscheduled: "st-none",
  };

  function batch() {
    return (state && state.batches || []).find((b) => b.id === currentId) || null;
  }

  async function load(selectId) {
    state = await api("/api/verify/batches");
    if (selectId == null && state.batch) selectId = state.batch.id;
    if (selectId == null && state.batches.length) selectId = state.batches[0].id;
    currentId = selectId;
    renderBatchSelect();
    render();
  }

  function renderBatchSelect() {
    const sel = $("#verify-batch-select");
    sel.innerHTML = state.batches.length
      ? state.batches.map((b) =>
          `<option value="${b.id}"${b.id === currentId ? " selected" : ""}>` +
          `${esc(b.plan_date)} · #${b.id} ${esc(b.name)}（${b.events.length} 条事件）</option>`
        ).join("")
      : `<option value="">（暂无批次，请新建）</option>`;
  }

  function render() {
    const b = batch();
    if (!b) {
      $("#verify-status-text").textContent = "该日期尚无核验批次，请点击「新建批次」并导入事件";
      $("#verify-status-meta").textContent = "";
      $("#verify-zone-rows").innerHTML = "";
      $("#verify-event-rows").innerHTML = "";
      $("#verify-ambiguous-list").innerHTML =
        `<li class="empty">无</li>`;
      $("#verify-unplanned-list").innerHTML = `<li class="empty">无</li>`;
      $("#verify-conflict-panel").classList.add("hidden");
      $("#plan-change-banner").classList.add("hidden");
      $("#verify-batch-meta").textContent = "";
      return;
    }
    $("#verify-batch-meta").textContent =
      `批次 #${b.id} · ${b.plan_date} · 创建于 ${(b.created || "").replace("T", " ").slice(0, 16)}`;

    const r = b.result;
    const sum = r.summary;
    $("#verify-status-text").innerHTML =
      `已核验 <b>${sum.event_count}</b> 条事件 / ${sum.zone_count} 个分区：` +
      `已灌 <b>${num(sum.delivered_l)}</b> L，缺口 <b>${num(sum.deficit_l)}</b> L` +
      (sum.ambiguous_l ? `，证据不足 <b>${num(sum.ambiguous_l)}</b> L` : "") +
      (sum.unplanned_l ? `，无计划运行 <b>${num(sum.unplanned_l)}</b> L` : "") +
      (sum.conflict_count ? `，<b>${sum.conflict_count}</b> 段实际冲突/越界` : "，无实际冲突");
    $("#verify-status-meta").textContent =
      `计划基线修订 ${b.base_revision}` +
      (b.plan_changed ? `（计划已变更至修订 ${b.current_revision}，结果按最新计划重算）`
                      : `（与当前计划一致）`);

    const banner = $("#plan-change-banner");
    if (b.plan_changed) {
      banner.textContent = `⚠ 批次基于修订 ${b.base_revision} 导入，当前计划已到修订 ${b.current_revision}：` +
        `核验结果已按最新计划全量重算，请对照台账复核归属变化。`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    const orphans = b.orphan_events || b.result.orphan_events || [];
    if (orphans.length) {
      banner.textContent = `🚫 有 ${orphans.length} 条事件的阀门已被删除` +
        `（${orphans.map((e) => e.event_id).join("、")}）：未参与核验，请删除或修正后再核。`;
      banner.classList.remove("hidden");
    } else if (!b.plan_changed) {
      banner.classList.add("hidden");
    }

    renderZones(r);
    renderConflicts(r);
    renderSegments(r);
    renderEvents(b);
  }

  function renderZones(r) {
    const tbody = $("#verify-zone-rows");
    tbody.innerHTML = "";
    r.zones.forEach((z) => {
      const tr = document.createElement("tr");
      tr.className = STATUS_CLASS[z.status] || "";
      const plan = z.planned
        ? `${z.planned.start.slice(11)}–${z.planned.end.slice(11)}` : "— 未编排 —";
      const timing = [];
      if (z.start_delta_min != null)
        timing.push(`起止偏差 ${fmtDelta(z.start_delta_min)}/${fmtDelta(z.end_delta_min)}`);
      if (z.interruptions.length)
        timing.push(`中断 ${z.interruptions.map((g) =>
          `${g.start.slice(11)}–${g.end.slice(11)}${g.ambiguous_minutes ? "(含证据不足 " + g.ambiguous_minutes + "分)" : ""}`
        ).join("、")}`);
      tr.innerHTML =
        `<td><div class="zname">${esc(z.zone_name)}</div>
           <div class="zmeta">${esc(z.valve_name)}</div></td>
         <td>${plan}</td>
         <td><span class="status-pill ${STATUS_CLASS[z.status]}">${z.status_label}</span></td>
         <td>${num(z.planned_water_l)}</td>
         <td>${num(z.delivered_l)}</td>
         <td>${z.deficit_l > 0 ? num(z.deficit_l) : "—"}</td>
         <td>${z.excess_l > 0 ? num(z.excess_l) : "—"}</td>
         <td>${z.early_minutes || "—"}</td>
         <td>${z.late_minutes || "—"}</td>
         <td>${(z.blackout_minutes + z.out_of_window_minutes)
              ? z.blackout_minutes + z.out_of_window_minutes +
                `<div class="zmeta">${z.blackout_minutes ? "停水" + z.blackout_minutes + " " : ""}` +
                `${z.out_of_window_minutes ? "窗口外" + z.out_of_window_minutes : ""}</div>`
              : "—"}</td>
         <td>${z.interrupted_minutes || "—"}<div class="zmeta">${timing.join("<br>")}</div></td>
         <td>${suggestionHtml(z)}</td>`;
      tbody.appendChild(tr);
    });
  }

  function fmtDelta(d) {
    if (d == null) return "—";
    if (d === 0) return "0";
    return (d > 0 ? "+" : "") + d + " 分";
  }

  function suggestionHtml(z) {
    const s = z.suggestion;
    if (!s) return `<span class="zmeta">无需补灌</span>`;
    const slots = s.slots.map((x) => x.start.slice(11) + "–" + x.end.slice(11)).join("、");
    if (s.feasible) {
      return `<div class="sug ok">✅ ${slots}</div>
        <div class="zmeta">可补 ${num(s.suppliable_l)} L（需 ${s.need_slots} 槽，不改原计划）</div>`;
    }
    return `<div class="sug no">⛔ 无完整可行解</div>
      <div class="zmeta">最多可补 ${num(s.suppliable_l)} L，最小缺口 ${num(s.residual_l)} L<br>
      可放槽：${slots || "无"}<br>
      阻塞原因：${s.reasons.length ? esc(s.reasons.join("；")) : "窗口内无剩余时段"}</div>`;
  }

  function renderConflicts(r) {
    const panel = $("#verify-conflict-panel");
    const ul = $("#verify-conflict-list");
    ul.innerHTML = "";
    if (!r.conflicts.length) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    r.conflicts.forEach((c) => {
      const li = document.createElement("li");
      li.innerHTML =
        `<span class="cf-type cf-${c.type}">${esc(c.type_label)}</span>
         <span class="where">${hm(c.start)}–${hm(c.end)}（${c.minutes} 分）</span>
         <span class="reason">${esc(c.detail)}</span>`;
      ul.appendChild(li);
    });
  }

  function segLine(s) {
    return `${esc(s.event_ids.join(" / "))} · ${hm(s.start)}–${hm(s.end)} · ${num(s.minutes)} 分 · ${num(s.water_l)} L` +
      (s.covered_zone_ids.length ? ` · 触及分区 ${s.covered_zone_ids.join(",")}` : "") +
      (s.in_blackout ? " · 停水段" : "") +
      (s.out_of_window ? " · 窗口外" : "");
  }

  function renderSegments(r) {
    const amb = $("#verify-ambiguous-list");
    amb.innerHTML = "";
    if (!r.ambiguous_segments.length) amb.innerHTML = `<li class="empty">无</li>`;
    r.ambiguous_segments.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = segLine(s);
      amb.appendChild(li);
    });
    const unp = $("#verify-unplanned-list");
    unp.innerHTML = "";
    if (!r.unplanned_segments.length) unp.innerHTML = `<li class="empty">无</li>`;
    r.unplanned_segments.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = segLine(s);
      unp.appendChild(li);
    });
  }

  function renderEvents(b) {
    const tbody = $("#verify-event-rows");
    tbody.innerHTML = "";
    b.events.forEach((e) => {
      const mins = Math.round(
        (new Date(e.end_ts) - new Date(e.start_ts)) / 60000);
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${esc(e.event_id)}</td><td>${esc(e.valve_name || ("#" + e.valve_id))}</td>
         <td>${hm(e.start_ts)}</td><td>${hm(e.end_ts)}</td>
         <td>${mins}</td><td>${num(e.flow_lmin)}</td>
         <td><button type="button" class="icon-btn del" title="删除事件并全量重算">🗑</button></td>`;
      tr.querySelector("button").addEventListener("click", () => removeEvent(e.event_id));
      tbody.appendChild(tr);
    });
  }

  async function removeEvent(eventId) {
    if (!confirm(`删除事件 ${eventId}？删除后将按剩余事件全量重算。`)) return;
    try {
      state = await api(`/api/verify/batches/${currentId}/events/${encodeURIComponent(eventId)}`,
                        { method: "DELETE" });
      renderBatchSelect();
      render();
      toast(`事件 ${eventId} 已删除，核验结果已全量重算`);
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 交互

  function parseImport(text, planDate) {
    const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (!lines.length) throw new Error("请先粘贴至少一行事件");
    const events = [];
    lines.forEach((line, i) => {
      const parts = line.split(/[,\t，;；]/).map((x) => x.trim());
      if (parts.length !== 5)
        throw new Error(`第 ${i + 1} 行：应为 5 列（event_id,阀门,开始,结束,流量）`);
      const [eid, valve, st, et, flow] = parts;
      let start = st, end = et;
      if (/^\d{1,2}:\d{2}$/.test(st)) start = `${planDate}T${st.padStart(5, "0")}`;
      if (/^\d{1,2}:\d{2}$/.test(et)) end = `${planDate}T${et.padStart(5, "0")}`;
      const payload = { event_id: eid, start, end, flow_lmin: Number(flow) };
      if (/^\d+$/.test(valve)) payload.valve_id = Number(valve);
      else payload.valve_name = valve;
      events.push(payload);
    });
    return events;
  }

  async function doImport() {
    const b = batch();
    if (!b) { toast("请先选择或新建核验批次", true); return; }
    let events;
    try {
      events = parseImport($("#verify-import-text").value, b.plan_date);
    } catch (e) { toast(e.message, true); return; }
    try {
      const data = await api(
        `/api/verify/batches/${b.id}/events`, { method: "POST", body: { events } });
      state = data;
      renderBatchSelect();
      render();
      toast(`导入完成：新增 ${data.imported || 0} 条，幂等跳过 ${data.skipped || 0} 条，已全量核验`);
    } catch (e) {
      if (e.data) {
        if (e.data.conflict === "event_content") {
          state = { batches: e.data.batches, batch: e.data.batch,
                    plan_dates: e.data.plan_dates };
          renderBatchSelect(); render();
          toast(`${e.message} —— 整批未写入`, true);
          return;
        }
      }
      toast(e.message, true);
    }
  }

  async function createBatch() {
    const dateStr = (window.__planDate || "").trim();
    const name = prompt("新批次名称：", "执行核验批次");
    if (name === null) return;
    try {
      const data = await api("/api/verify/batches", {
        method: "POST",
        body: { name: name || "执行核验批次", plan_date: dateStr || undefined },
      });
      state = data;
      currentId = data.batch ? data.batch.id :
        (data.batches[0] && data.batches[0].id);
      renderBatchSelect();
      render();
      toast("批次已创建，请粘贴事件导入");
    } catch (e) { toast(e.message, true); }
  }

  async function deleteBatch() {
    const b = batch();
    if (!b) return;
    if (!confirm(`删除核验批次「${b.name}」及其全部事件？`)) return;
    try {
      state = await api(`/api/verify/batches/${b.id}`, { method: "DELETE" });
      currentId = state.batches.length ? state.batches[0].id : null;
      renderBatchSelect();
      render();
      toast("核验批次已删除");
    } catch (e) { toast(e.message, true); }
  }

  function bind() {
    $("#verify-batch-select").addEventListener("change", (e) => {
      currentId = e.target.value ? Number(e.target.value) : null;
      render();
    });
    $("#btn-verify-import").addEventListener("click", doImport);
    $("#btn-verify-new").addEventListener("click", createBatch);
    $("#btn-verify-delete-batch").addEventListener("click", deleteBatch);
    $("#btn-verify-save").addEventListener("click", () => {
      const b = batch();
      if (!b) { toast("请先选择批次", true); return; }
      window.location.href = `/api/verify/batches/${b.id}/export`;
    });
  }

  window.VerifyView = {
    bind, load,
    setPlanDate(d) { window.__planDate = d; },
  };
})();
