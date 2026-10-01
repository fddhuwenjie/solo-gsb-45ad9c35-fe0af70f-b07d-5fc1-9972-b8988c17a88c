/* 执行核验 —— 原生 JS */
(() => {
  "use strict";

  let state = null;

  const $ = (sel, root = document) => root.querySelector(sel);

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 3500);
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
  const fmtNum = (v, d = 1) =>
    (v === null || v === undefined) ? "—"
      : (Math.round(v * 10 ** d) / 10 ** d).toString();
  const fmtTs = (ts) => String(ts || "").replace("T", " ").replace("T24:00", "24:00");
  const hhmm = (ts) => (ts || "").slice(11, 16);

  // ------------------------------------------------------------ 加载

  function stateUrl() {
    const p = new URLSearchParams();
    if (state && state.selected_id) p.set("batch_id", state.selected_id);
    const day = $("#filter-date").value;
    if (day) p.set("date", day);
    const qs = p.toString();
    return "/api/verify/state" + (qs ? "?" + qs : "");
  }

  async function load(keepSelection = true) {
    const prev = state ? state.selected_id : null;
    const url = keepSelection && prev
      ? `/api/verify/state?batch_id=${prev}` : stateUrl();
    state = await api(url);
    if (!state.selected_id && state.batches.length) {
      state = await api(`/api/verify/state?batch_id=${state.batches[0].id}`);
    }
    renderAll();
  }

  function renderAll() {
    renderBatchList();
    renderBatchForm();
    renderValveOptions();
    renderOverview();
    renderConflicts();
    renderMakeup();
    renderZones();
    renderSegments();
    renderEvents();
  }

  // ------------------------------------------------------------ 批次

  function renderBatchList() {
    const ul = $("#batch-list");
    ul.innerHTML = "";
    if (!state.batches.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">暂无核验批次，请在下方新建</li>`;
      return;
    }
    state.batches.forEach((b) => {
      const li = document.createElement("li");
      li.className = "entity-row clickable" +
        (b.id === state.selected_id ? " selected" : "");
      li.innerHTML =
        `<span class="grow">
           <span class="line1"><span class="name">${escapeHtml(b.name)}</span></span>
           <span class="meta">${b.plan_date} · ${b.event_count} 条事件</span>
         </span>`;
      li.addEventListener("click", () => selectBatch(b.id));
      ul.appendChild(li);
    });
  }

  function renderBatchForm() {
    const f = $("#batch-form");
    if (!f.elements.plan_date.value) {
      f.elements.plan_date.value = state.plan_date || "";
    }
    const sel = state.selected;
    $("#btn-batch-delete").disabled = !sel;
    $("#btn-export").disabled = !sel;
    $("#import-card").style.opacity = sel ? "1" : ".55";
  }

  function renderValveOptions() {
    const sel = $("#event-form").elements.valve_id;
    sel.innerHTML = state.valves.length
      ? state.valves.map((v) =>
          `<option value="${v.id}">${escapeHtml(v.name)}（${v.flow_lmin} L/min）</option>`).join("")
      : `<option value="">（请先建立阀门）</option>`;
  }

  async function selectBatch(id) {
    try {
      state = await api(`/api/verify/state?batch_id=${id}`);
      renderAll();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 总览

  function renderOverview() {
    const sel = state.selected;
    const title = $("#verify-title"), badge = $("#verify-badge");
    const meta = $("#verify-meta"), stats = $("#verify-stats");
    const warn = $("#revision-warn");
    stats.innerHTML = "";
    badge.className = "case-badge";
    warn.classList.add("hidden");
    if (!sel) {
      title.textContent = "未选择批次";
      badge.textContent = "";
      meta.textContent = "在左侧新建批次并导入阀门运行事件后，系统将全量核验。";
      return;
    }
    const s = sel.result.summary;
    title.textContent = `${sel.name}（${sel.plan_date}）`;
    const hasProblem = s.deficit_l > 0 || s.conflict_count > 0
      || s.ambiguous_count > 0 || s.makeup_unsolved_l > 0;
    if (hasProblem) {
      badge.classList.add("bad");
      badge.textContent = "存在待处理问题";
    } else if (s.out_of_plan_count > 0 || s.surplus_l > 0) {
      badge.classList.add("warn");
      badge.textContent = "有越界 / 超灌";
    } else {
      badge.classList.add("ok");
      badge.textContent = "核验通过";
    }
    meta.innerHTML =
      `批次 #${sel.id} · 保存于 ${fmtTs(sel.created)} · ` +
      `${s.event_count} 条事件 / ${s.scheduled_count} 个已编排分区` +
      (sel.note ? ` · ${escapeHtml(sel.note)}` : "");
    if (sel.plan_revision !== null
        && sel.plan_revision !== sel.current_revision) {
      warn.classList.remove("hidden");
      warn.innerHTML = `⚠ 本批次保存时计划修订为 <b>${sel.plan_revision}</b>，`
        + `当前计划已到修订 <b>${sel.current_revision}</b>：核验按<strong>当前计划</strong>`
        + `实时重算，如需固定当时结果请以导出 JSON 留档。`;
    }

    const items = [
      ["已灌水量", s.delivered_l, "L"],
      ["需水总量", s.target_l, "L"],
      ["缺水合计", s.deficit_l, "L"],
      ["超灌合计", s.surplus_l, "L"],
      ["计划外水量", s.out_of_plan_l, "L"],
      ["证据不足水量", s.ambiguous_l, "L"],
      ["运行冲突", s.conflict_count, "段"],
      ["补灌剩余缺口", s.makeup_unsolved_l, "L"],
    ];
    items.forEach(([label, val, unit]) => {
      const li = document.createElement("li");
      const danger = (label === "缺水合计" && val > 0)
        || (label === "运行冲突" && val > 0)
        || (label === "补灌剩余缺口" && val > 0)
        || (label === "证据不足水量" && val > 0);
      li.innerHTML = `<span class="stat-label">${label}</span>`
        + `<span class="stat-val"${danger ? ' style="color:var(--red)"' : ""}>`
        + `${fmtNum(val)} <span class="stat-unit">${unit}</span></span>`;
      stats.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 冲突

  function renderConflicts() {
    const card = $("#conflict-card"), tbody = $("#conflict-rows");
    tbody.innerHTML = "";
    const sel = state.selected;
    const list = sel ? sel.result.conflicts : [];
    card.classList.toggle("hidden", !list.length);
    list.forEach((c) => {
      const tr = document.createElement("tr");
      const isTotal = c.type === "total_flow";
      tr.innerHTML =
        `<td class="${isTotal ? "conflict-total" : "conflict-valve"}">`
        + `${isTotal ? "总流量超限" : "阀门并发超限"}</td>`
        + `<td>${isTotal ? "（全系统）" : escapeHtml(c.valve_name || "")}</td>`
        + `<td>${c.start}</td><td>${c.end}</td><td>${c.minutes}</td>`
        + `<td>${isTotal
            ? `${fmtNum(c.flow_lmin)} / ${fmtNum(c.limit)} L/min`
            : `${c.count} / ${c.limit} 路`}</td>`
        + `<td>${c.event_ids.map(escapeHtml).join("、")}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 补灌建议

  function renderMakeup() {
    const card = $("#makeup-card"), box = $("#makeup-list");
    box.innerHTML = "";
    const sel = state.selected;
    const list = sel ? sel.result.makeup : [];
    card.classList.toggle("hidden", !list.length);
    list.forEach((m) => {
      const div = document.createElement("div");
      div.className = "makeup-item" + (m.feasible ? "" : " infeasible");
      const blocks = m.blocks.length
        ? m.blocks.map((b) =>
            `<span class="slot-chip">${b.start}–${b.end}（${b.duration_min} min）</span>`).join("")
        : `<span class="slot-chip gap">无可安排时段</span>`;
      let blocked = "";
      if (m.blocked.length) {
        blocked = `<ul class="blocked-list">`
          + m.blocked.map((b) =>
              `<li>${escapeHtml(b.reason)}：${b.slot_count} 个槽</li>`).join("")
          + `</ul>`;
      }
      div.innerHTML =
        `<div class="makeup-head">
           <span class="zname">${escapeHtml(m.zone_name)}</span>
           <span class="makeup-badge ${m.feasible ? "ok" : "no"}">`
           + `${m.feasible ? "✔ 可补足" : "✖ 无法补足"}</span>
           <span>缺水 <b>${fmtNum(m.deficit_l)}</b> L（需 ${m.need_slots} 槽）</span>
           <span>${escapeHtml(m.valve_name)} · ${fmtNum(m.flow_lmin)} L/min</span>
         </div>
         <div class="makeup-detail">
           <div>建议补灌时段（原计划保持不变）：${blocks}</div>
           <div>剩余允许窗口：${m.free_windows.map((w) =>
             `${w.start}–${w.end}`).join("、") || "无"}
             · 共 ${m.free_slot_count} 个可用槽</div>
           ${m.feasible
             ? `<div>预计补入 ${fmtNum(m.delivered_l)} L，缺水可清零。</div>`
             : `<div class="num-deficit">最小缺口：仍缺 ${fmtNum(m.remaining_l)} L。`
               + `${escapeHtml(m.reason || "")}</div>`}
           ${blocked}
         </div>`;
      box.appendChild(div);
    });
  }

  // ------------------------------------------------------------ 分区台账

  function renderZones() {
    const tbody = $("#zone-rows");
    tbody.innerHTML = "";
    const sel = state.selected;
    if (!sel) {
      tbody.innerHTML = `<tr><td colspan="13" style="color:var(--ink-soft)">未选择批次</td></tr>`;
      return;
    }
    const labels = sel.result.status_labels;
    sel.result.zones.forEach((z) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(z.zone_name)}${z.scheduled ? "" : ' <small style="color:var(--ink-soft)">（未编排）</small>'}</td>`
        + `<td>${escapeHtml(z.valve_name)}</td>`
        + `<td><span class="zone-status ${z.status}">${labels[z.status]}</span></td>`
        + `<td>${z.plan_start || "—"}–${z.plan_end || "—"}</td>`
        + `<td>${z.actual_start || "—"}–${z.actual_end || "—"}</td>`
        + `<td>${fmtNum(z.target_l, 0)}</td>`
        + `<td>${fmtNum(z.delivered_l)}</td>`
        + `<td class="${z.deficit_l > 0 ? "num-deficit" : ""}">${fmtNum(z.deficit_l)}</td>`
        + `<td class="${z.surplus_l > 0 ? "num-surplus" : ""}">${fmtNum(z.surplus_l)}</td>`
        + `<td class="${z.early_min ? "num-warn" : ""}">${z.early_min || ""}</td>`
        + `<td class="${z.late_min ? "num-warn" : ""}">${z.late_min || ""}</td>`
        + `<td class="${z.interrupt_min ? "num-warn" : ""}">${z.interrupt_min || ""}</td>`
        + `<td class="${z.out_of_plan_l > 0 ? "num-surplus" : ""}">${fmtNum(z.out_of_plan_l)}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 对齐明细

  function segRow(s) {
    const cands = s.candidate_zones && s.candidate_zones.length
      ? `<div class="seg-cands">候选分区：${s.candidate_zones.map((c) =>
          escapeHtml(c.zone_name)).join("、")}（无法唯一归属，不重复计量）</div>` : "";
    const adj = s.adjacency === "prefix" ? " · 提前运行"
      : s.adjacency === "suffix" ? " · 延后运行"
      : s.adjacency === "gap" ? " · 两计划之间"
      : s.adjacency === "detached" ? " · 与计划不相邻" : "";
    return `<div class="seg-chip ${s.kind === AMBIGUOUS ? "ambiguous" : "out"}">
        <span class="seg-time">${s.start}–${s.end}</span>（${s.minutes} min，
        ${fmtNum(s.water_l)} L）· 事件 <b>${escapeHtml(s.event_id)}</b> ·
        ${escapeHtml(s.valve_name || "")}${adj}${cands}
      </div>`;
  }
  const AMBIGUOUS = "ambiguous";

  function renderSegments() {
    const card = $("#segment-card");
    const amb = state.selected ? state.selected.result.ambiguous_segments : [];
    const out = state.selected ? state.selected.result.out_of_plan_segments : [];
    card.classList.toggle("hidden", !amb.length && !out.length);
    const ab = $("#ambiguous-box"), ob = $("#out-box");
    ab.innerHTML = amb.length
      ? `<div class="seg-subbox"><h3>🟠 证据不足子段（${amb.length}）</h3>`
        + amb.map(segRow).join("") + `</div>` : "";
    ob.innerHTML = out.length
      ? `<div class="seg-subbox"><h3>🔴 计划外 / 越界子段（${out.length}）</h3>`
        + out.map(segRow).join("") + `</div>` : "";
  }

  // ------------------------------------------------------------ 事件台账

  function renderEvents() {
    const card = $("#event-card"), tbody = $("#event-rows");
    tbody.innerHTML = "";
    const sel = state.selected;
    card.classList.toggle("hidden", !sel || !sel.result.events.length);
    if (!sel) return;
    sel.result.events.forEach((e) => {
      const kinds = {};
      e.segments.forEach((s) => { kinds[s.kind] = (kinds[s.kind] || 0) + 1; });
      const chips = Object.entries(kinds).map(([k, n]) =>
        `<span class="event-kind ${k}">${sel.result.status_labels[k] || k}×${n}</span>`).join("");
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(e.event_id)}</td>`
        + `<td>${escapeHtml(e.valve_name || "")}</td>`
        + `<td>${hhmm(e.start)}</td><td>${e.end.endsWith("24:00") ? "24:00" : hhmm(e.end)}</td>`
        + `<td>${e.duration_min}</td><td>${fmtNum(e.flow_lmin)}</td>`
        + `<td>${chips || "—"}</td>`
        + `<td><button class="del-btn" title="删除事件后全量重算">🗑</button></td>`;
      tr.querySelector(".del-btn").addEventListener("click", () => removeEvent(e));
      tbody.appendChild(tr);
    });
  }

  async function removeEvent(e) {
    if (!confirm(`删除事件 ${e.event_id}（${hhmm(e.start)}–${hhmm(e.end)}）？删除后全量重算。`)) return;
    if (!e.id) { toast("缺少事件主键，请刷新页面", true); return; }
    try {
      await api(`/api/verify/events/${e.id}`, { method: "DELETE" });
      await load();
      toast("事件已删除，已全量重算");
    } catch (err) { toast(err.message, true); }
  }

  // ------------------------------------------------------------ 表单

  function nowInput(deltaDay = 0) {
    const d = new Date();
    d.setDate(d.getDate() + deltaDay);
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
      + `T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function bindForms() {
    $("#batch-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        const s = await api("/api/verify/batches", {
          method: "POST",
          body: {
            name: f.elements.name.value.trim(),
            plan_date: f.elements.plan_date.value,
            note: f.elements.note.value.trim(),
          },
        });
        f.elements.name.value = "";
        f.elements.note.value = "";
        state = await api(`/api/verify/state?batch_id=${s.selected_id}`);
        renderAll();
        toast("核验批次已创建");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-batch-delete").addEventListener("click", async () => {
      const sel = state.selected;
      if (!sel) return;
      if (!confirm(`删除核验批次「${sel.name}」？其全部运行事件将一并删除。`)) return;
      try {
        await api(`/api/verify/batches/${sel.id}`, { method: "DELETE" });
        state = await api("/api/verify/state?date=" + ($("#filter-date").value || ""));
        if (!state.selected_id && state.batches.length) {
          state = await api(`/api/verify/state?batch_id=${state.batches[0].id}`);
        }
        renderAll();
        toast("批次已删除");
      } catch (err) { toast(err.message, true); }
    });

    $("#event-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.selected_id) { toast("请先选择核验批次", true); return; }
      const f = e.target;
      try {
        const data = await api(
          `/api/verify/batches/${state.selected_id}/events`, {
            method: "POST",
            body: { events: [{
              event_id: f.elements.event_id.value.trim(),
              valve_id: Number(f.elements.valve_id.value),
              start: f.elements.start.value,
              end: f.elements.end.value,
              flow_lmin: Number(f.elements.flow_lmin.value),
            }] },
          });
        f.elements.event_id.value = "";
        await load();
        toast(data.skipped ? "相同事件已存在，幂等跳过；已全量重算"
          : "事件已导入，已全量重算");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-batch-import").addEventListener("click", async () => {
      if (!state.selected_id) { toast("请先选择核验批次", true); return; }
      const raw = $("#batch-json").value.trim();
      if (!raw) { toast("请粘贴事件 JSON 数组", true); return; }
      let events;
      try {
        events = JSON.parse(raw);
        if (!Array.isArray(events)) throw new Error("顶层必须为数组");
      } catch (err) { toast("JSON 解析失败：" + err.message, true); return; }
      try {
        const data = await api(
          `/api/verify/batches/${state.selected_id}/events`, {
            method: "POST", body: { events },
          });
        $("#batch-json").value = "";
        await load();
        toast(`导入完成：新增 ${data.imported} 条，幂等跳过 ${data.skipped} 条`);
      } catch (err) { toast(err.message, true); }
    });

    $("#filter-date").addEventListener("change", async () => {
      try {
        const day = $("#filter-date").value;
        state = await api("/api/verify/state" + (day ? `?date=${day}` : ""));
        if (!state.selected_id && state.batches.length) {
          state = await api(`/api/verify/state?batch_id=${state.batches[0].id}`);
        }
        renderAll();
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-export").addEventListener("click", () => {
      if (state.selected_id) {
        window.location.href =
          `/api/verify/export?batch_id=${state.selected_id}`;
      }
    });
  }

  // ------------------------------------------------------------ 启动

  bindForms();
  load(false).then(() => {
    const f = $("#event-form");
    const d = state.selected ? state.selected.plan_date : state.plan_date;
    if (d) {
      f.elements.start.value = d + "T08:00";
      f.elements.end.value = d + "T08:30";
    }
  }).catch((e) => toast(e.message, true));
})();
