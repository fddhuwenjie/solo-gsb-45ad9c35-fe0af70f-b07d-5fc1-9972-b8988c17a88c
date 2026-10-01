/* 病害接触追踪 —— 原生 JS */
(() => {
  "use strict";

  const TYPE_TEXT = { diagnosis: "确诊", visit: "到访", disinfection: "消毒" };
  const TYPE_CLASS = { diagnosis: "diagnosis", visit: "visit", disinfection: "disinfection" };

  let state = null;
  let selectedId = null;
  const filter = { type: "", tool_id: "", zone_id: "", quarantineOnly: false };

  const $ = (sel, root = document) => root.querySelector(sel);

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

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtTs(ts) { return String(ts ?? "").replace("T", " "); }
  function nowLocalInput() {
    const d = new Date();
    d.setSeconds(0, 0);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  const sel = () => state.selected;
  const result = () => sel()?.result;
  const frozen = () => sel()?.status === "frozen";

  // ------------------------------------------------------------ 加载

  async function load(id = selectedId) {
    const qs = id ? `?case_id=${id}` : "";
    state = await api("/api/disease/state" + qs);
    selectedId = state.selected_id;
    renderAll();
  }

  function renderAll() {
    renderCases();
    renderTools();
    renderCaseForm();
    renderEventFormMeta();
    renderOverview();
    renderZoneTable();
    renderQuarantine();
    renderEvents();
  }

  // ------------------------------------------------------------ 案件列表

  function renderCases() {
    const ul = $("#case-list");
    ul.innerHTML = "";
    if (!state.cases.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">暂无案件，请先新建</li>`;
      return;
    }
    state.cases.forEach((cs) => {
      const li = document.createElement("li");
      li.className = "entity-row case-item" + (cs.id === selectedId ? " selected" : "");
      li.innerHTML =
        `<span class="row1">
           <span class="grow">
             <span class="name">${escapeHtml(cs.disease_name)}</span>
             <div class="meta">
               <span class="case-badge ${cs.status}">${cs.status === "frozen" ? "🔒 已冻结" : "调查中"}</span>
               <span>${cs.event_count} 个事件 · 潜伏 ${fmtNum(cs.latency_hours)} h</span>
             </div>
           </span>
           <button class="icon-btn edit-case" title="编辑案件">✎</button>
         </span>`;
      li.addEventListener("click", (e) => {
        if (e.target.closest(".edit-case")) {
          editCase(cs);
          return;
        }
        selectedId = cs.id;
        load(cs.id).catch((err) => toast(err.message, true));
      });
      ul.appendChild(li);
    });
  }

  function fmtNum(v) {
    return String(Math.round(Number(v) * 100) / 100);
  }

  function editCase(cs) {
    const f = $("#case-form");
    f.elements.id.value = cs.id;
    f.elements.disease_name.value = cs.disease_name;
    f.elements.latency_hours.value = cs.latency_hours;
    f.elements.note.value = cs.note || "";
    $("#case-submit").textContent = "保存修改";
    $("#case-cancel").classList.remove("hidden");
  }

  function renderCaseForm() {
    const f = $("#case-form");
    const editing = Number(f.elements.id.value) === selectedId && $("#case-submit").textContent.includes("保存");
    f.classList.toggle("locked", frozen() && editing);
    $("#case-ops").style.display = sel() ? "flex" : "none";
    if (frozen()) {
      $("#btn-case-freeze").disabled = true;
      $("#btn-case-freeze").textContent = "🔒 已冻结（快照不可改）";
    } else {
      $("#btn-case-freeze").disabled = !sel();
      $("#btn-case-freeze").textContent = "🔒 确认并冻结案件快照";
    }
  }

  // ------------------------------------------------------------ 工具

  function renderTools() {
    const ul = $("#tool-list");
    ul.innerHTML = "";
    const contaminated = new Set();
    if (result()) {
      result().tools.forEach((t) => { if (t.contaminated) contaminated.add(t.tool_id); });
    }
    if (!state.tools.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">暂无工具，请先添加</li>`;
      return;
    }
    state.tools.forEach((t) => {
      const li = document.createElement("li");
      li.className = "entity-row tool-item" + (t.deleted ? " deleted-tool" : "");
      li.innerHTML =
        `<span class="grow">
           <span class="name">${contaminated.has(t.id) ? "☣ " : ""}${escapeHtml(t.name)}</span>
           <div class="meta">${t.deleted ? '<span class="deleted-tag">已删除 · 冻结案件历史</span>'
                                         : (t.retired ? '<span class="retired-tag">已停用</span>' : "")}
             ${contaminated.has(t.id) ? '<span style="color:var(--red)">重放截止时仍携带污染</span>'
             : t.deleted ? "历史引用保留" : "重放截止时无污染"}
             ${t.note ? " · " + escapeHtml(t.note) : ""}</div>
         </span>`;
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 顶部概况 + 筛选器

  function renderOverview() {
    const cs = sel();
    const badge = $("#case-badge");
    if (!cs) {
      $("#case-title").textContent = "未选择案件";
      badge.textContent = "";
      badge.className = "case-badge";
      $("#case-meta").textContent = "请在左侧新建或选择一个病害案件。";
    } else {
      $("#case-title").textContent = `案件 #${cs.id} · ${cs.disease_name}`;
      badge.className = "case-badge " + cs.status;
      badge.textContent = cs.status === "frozen" ? "🔒 已冻结快照" : "调查中（可补录 / 删除事件）";
      const r = result();
      $("#case-meta").innerHTML =
        `<span>潜伏时长 <b>${fmtNum(cs.latency_hours)} 小时</b></span>` +
        `<span>重放截止 ${r && r.cutoff_ts ? fmtTs(r.cutoff_ts) : "—"}</span>` +
        `<span>待隔离分区 <b style="color:var(--amber)">${r ? r.quarantine.length : 0}</b> 个</span>` +
        (cs.frozen_at ? `<span>冻结于 ${fmtTs(cs.frozen_at)}</span>` : "") +
        (cs.note ? `<span>${escapeHtml(cs.note)}</span>` : "");
    }

    const tsel = $("#filter-tool");
    const zsel = $("#filter-zone");
    const keepT = tsel.value, keepZ = zsel.value;
    tsel.innerHTML = `<option value="">全部工具</option>` +
      state.tools.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}${t.deleted ? "（已删除）" : ""}</option>`).join("");
    zsel.innerHTML = `<option value="">全部分区</option>` +
      state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("");
    tsel.value = keepT; zsel.value = keepZ;
    [tsel, zsel, $("#filter-type")].forEach((el) => { el.disabled = !cs; });
    $("#filter-quarantine").checked = filter.quarantineOnly;
  }

  // ------------------------------------------------------------ 事件表单

  function renderEventFormMeta() {
    const cs = sel();
    const toolSel = $("#event-tool");
    const zoneSel = $("#event-zone");
    const usableTools = state.tools.filter((t) => !t.deleted);
    toolSel.innerHTML = usableTools.length
      ? usableTools.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("")
      : `<option value="">（请先添加工具）</option>`;
    zoneSel.innerHTML = state.zones.length
      ? state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("")
      : `<option value="">（请先在轮灌台添加分区）</option>`;
    syncEventTypeFields();
    $("#event-card").classList.toggle("locked", frozen() || !cs);
    if (!cs) $("#event-card-sub").textContent = "请先选择案件";
    else if (frozen()) $("#event-card-sub").textContent = "案件已冻结，事件只读";
    else $("#event-card-sub").textContent = "按确诊 / 到访 / 消毒顺序重放";
  }

  function syncEventTypeFields() {
    const type = $("#event-type").value;
    $("#event-form .field-tool").classList.toggle("hidden", type === "diagnosis");
    $("#event-form .field-zone").classList.toggle("hidden", type === "disinfection");
    $("#event-form .field-qualified").classList.toggle("hidden", type !== "disinfection");
  }

  function resetEventForm() {
    const f = $("#event-form");
    f.reset();
    f.elements.id.value = "";
    $("#event-type").value = "diagnosis";
    f.elements.ts.value = nowLocalInput();
    $("#event-submit").textContent = "登记事件";
    $("#event-cancel").classList.add("hidden");
    syncEventTypeFields();
  }

  // ------------------------------------------------------------ 分区结果表 + 最短事件链

  function chainHtml(chain) {
    if (!chain || !chain.length) return `<span class="dash">—</span>`;
    return `<span class="chain">` + chain.map((e) => {
      const cls = ["ev-chip", TYPE_CLASS[e.type]];
      if (e.type === "disinfection" && e.qualified) cls.push("ok");
      let who;
      if (e.type === "diagnosis") who = `确诊 ${e.zone_name || "?"}`;
      else if (e.type === "visit") who = `${e.tool_name || "?"} → ${e.zone_name || "?"}`;
      else who = `${e.tool_name || "?"}${e.qualified ? " 合格消毒" : " 消毒不合格"}`;
      return `<span class="${cls.join(" ")}" title="#${e.id} ${fmtTs(e.ts)}">` +
        `${escapeHtml(who)}</span>`;
    }).join(`<span class="arrow">➜</span>`) + `</span>`;
  }

  function renderZoneTable() {
    const tbody = $("#zone-rows");
    tbody.innerHTML = "";
    const r = result();
    if (!sel()) {
      tbody.innerHTML = emptyRow(6, "请先选择案件");
      return;
    }
    let rows = r.zones;
    if (filter.quarantineOnly) rows = rows.filter((z) => z.quarantine);
    if (filter.zone_id) rows = rows.filter((z) => String(z.zone_id) === filter.zone_id);
    if (!rows.length) {
      tbody.innerHTML = emptyRow(6, "没有符合筛选条件的分区");
      return;
    }
    rows.forEach((z) => {
      const tr = document.createElement("tr");
      if (z.quarantine) tr.className = "row-quarantine";
      let statePill;
      if (z.state === "seed") statePill = `<span class="state-pill seed">☣ 确诊源头</span>`;
      else if (z.state === "exposed")
        statePill = z.mature
          ? `<span class="state-pill mature">☣ 已暴露 · 具传染性</span>`
          : `<span class="state-pill latent">⏳ 暴露潜伏中</span>`;
      else statePill = `<span class="state-pill clean">清洁</span>`;
      tr.innerHTML =
        `<td><b>${escapeHtml(z.zone_name)}</b></td>
         <td>${statePill}</td>
         <td>${z.exposed_at ? fmtTs(z.exposed_at) : '<span class="dash">—</span>'}</td>
         <td>${z.mature_at
              ? fmtTs(z.mature_at) + (z.mature ? ' <span class="state-pill mature">已到</span>'
                                                : ' <span class="state-pill latent">未到</span>')
              : '<span class="dash">—</span>'}</td>
         <td>${chainHtml(z.chain)}</td>
         <td>${z.quarantine ? '<span class="state-pill exposed">🛑 隔离</span>' : ""}</td>`;
      tbody.appendChild(tr);
    });
  }

  function emptyRow(n, msg) {
    return `<tr class="empty-row"><td colspan="${n}" style="text-align:center;color:var(--ink-soft);padding:16px">${msg}</td></tr>`;
  }

  // ------------------------------------------------------------ 待隔离清单

  function renderQuarantine() {
    const ul = $("#quarantine-list");
    const sub = $("#quarantine-sub");
    ul.innerHTML = "";
    const r = result();
    if (!sel()) { sub.textContent = ""; ul.innerHTML = `<li class="empty" style="border:none;background:none">—</li>`; return; }
    sub.textContent = `共 ${r.quarantine.length} 个分区需隔离观察`;
    if (!r.quarantine.length) {
      ul.innerHTML = `<li class="empty" style="border:none;background:none">无暴露分区，无需隔离</li>`;
      return;
    }
    r.quarantine.forEach((q) => {
      const li = document.createElement("li");
      li.className = q.state === "seed" ? "seed" : "";
      li.innerHTML =
        `<span class="q-head">🛑 ${escapeHtml(q.zone_name)}
           <span class="state-pill ${q.state === "seed" ? "seed" : q.mature ? "mature" : "latent"}">` +
        (q.state === "seed" ? "确诊源头" : q.mature ? "具传染性" : "潜伏中") + `</span></span>
         <span class="q-meta">最早暴露：${fmtTs(q.exposed_at)}</span>
         <span class="q-meta">可传染时间：${fmtTs(q.mature_at)}${q.mature ? "（已到）" : "（未到）"}</span>
         <span class="q-meta">最短事件链 ${q.chain_length} 步</span>`;
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------ 时间线 + 事件表

  function eventPassesFilter(e) {
    if (filter.type && e.type !== filter.type) return false;
    if (filter.tool_id && String(e.tool_id ?? "") !== filter.tool_id) return false;
    if (filter.zone_id && String(e.zone_id ?? "") !== filter.zone_id) return false;
    return true;
  }

  function renderEvents() {
    const tbody = $("#event-rows");
    const trace = $("#replay-trace");
    const sub = $("#timeline-sub");
    tbody.innerHTML = "";
    trace.innerHTML = "";
    const r = result();
    if (!sel()) {
      sub.textContent = "";
      tbody.innerHTML = emptyRow(7, "请先选择案件");
      trace.innerHTML = `<div class="replay-empty">—</div>`;
      return;
    }

    // 重放过程（按重放顺序 = ts, 类型序, id），与筛选联动
    const effectsById = new Map(r.effects.map((x) => [x.event_id, x]));
    const replayOrder = [...state.events].sort((a, b) =>
      a.ts.localeCompare(b.ts) ||
      ({ diagnosis: 0, visit: 1, disinfection: 2 }[a.type] - { diagnosis: 0, visit: 1, disinfection: 2 }[b.type]) ||
      a.id - b.id);
    let shownTrace = 0;
    replayOrder.forEach((e) => {
      if (!eventPassesFilter(e)) return;
      const x = effectsById.get(e.id);
      const actions = (x && x.actions) || [];
      const silent = actions.length === 0 ||
        actions.every((a) => a.includes("无传播") || a.includes("仅作记录"));
      const line = document.createElement("div");
      line.className = "replay-line" + (silent ? " silent" : "");
      const head = `${fmtTs(e.ts)} · ${e.tool_name || (e.zone_name || "")}${e.type === "visit" ? " → " + (e.zone_name || "") : ""}`;
      line.innerHTML =
        `<span class="r-time">${fmtTs(e.ts)}</span>` +
        `<span class="r-type ${TYPE_CLASS[e.type]}">${TYPE_TEXT[e.type]}</span>` +
        `<span class="r-action">${escapeHtml(head.split(" · ")[1] || "")}` +
        (actions.length ? ` —— ${actions.map(escapeHtml).join("；")}` : "") + `</span>`;
      trace.appendChild(line);
      shownTrace++;
    });
    if (!shownTrace) trace.innerHTML = `<div class="replay-empty">没有符合筛选条件的事件</div>`;

    // 事件明细表（时间倒序）
    const rows = state.events.filter(eventPassesFilter).slice().reverse();
    sub.textContent = `共 ${state.events.length} 个事件，显示 ${rows.length} 个（上方为重放过程）`;
    if (!rows.length) {
      tbody.innerHTML = emptyRow(7, "没有符合筛选条件的事件");
      return;
    }
    rows.forEach((e) => {
      const tr = document.createElement("tr");
      const qual = e.type === "disinfection"
        ? (e.qualified ? '<span class="yes">✔ 合格</span>' : '<span class="no">✘ 不合格</span>')
        : '<span class="dash">—</span>';
      tr.innerHTML =
        `<td>${fmtTs(e.ts)}</td>
         <td><span class="state-pill ${TYPE_CLASS[e.type]}" style="background:none;border:none;padding:0">${TYPE_TEXT[e.type]}</span></td>
         <td>${escapeHtml(e.tool_name || "—")}</td>
         <td>${escapeHtml(e.zone_name || "—")}</td>
         <td>${qual}</td>
         <td>${escapeHtml(e.note || "")}</td>
         <td class="row-actions">
           <button class="edit-btn" title="编辑事件">✎</button>
           <button class="del-btn" title="删除事件（删除后重算）">🗑</button>
         </td>`;
      tr.querySelector(".edit-btn").addEventListener("click", () => editEvent(e));
      tr.querySelector(".del-btn").addEventListener("click", () => removeEvent(e));
      if (frozen()) {
        tr.querySelectorAll("button").forEach((b) => (b.disabled = true));
      }
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 案件表单 / 冻结 / 删除

  function bindCaseForms() {
    $("#case-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const id = f.elements.id.value;
      const body = {
        disease_name: f.elements.disease_name.value.trim(),
        latency_hours: Number(f.elements.latency_hours.value),
        note: f.elements.note.value.trim(),
      };
      try {
        if (id && Number(id) === selectedId) {
          await api(`/api/disease/cases/${id}`, { method: "PUT", body });
          toast("案件已更新，结果按新潜伏时长重算");
        } else {
          const s = await api("/api/disease/cases", { method: "POST", body });
          selectedId = s.selected_id;
          toast("案件已创建");
        }
        f.reset();
        f.elements.id.value = "";
        f.elements.latency_hours.value = 24;
        $("#case-submit").textContent = "新建案件";
        $("#case-cancel").classList.add("hidden");
        await load(selectedId);
      } catch (err) { toast(err.message, true); }
    });

    $("#case-cancel").addEventListener("click", () => {
      const f = $("#case-form");
      f.reset();
      f.elements.id.value = "";
      f.elements.latency_hours.value = 24;
      $("#case-submit").textContent = "新建案件";
      $("#case-cancel").classList.add("hidden");
    });

    $("#btn-case-freeze").addEventListener("click", async () => {
      if (!sel() || frozen()) return;
      if (!confirm(`确认冻结案件「${sel().disease_name}」？\n\n` +
                   "冻结时把当前重算结果（各分区最早暴露、最短事件链、待隔离清单）" +
                   "保存为案件快照；冻结后不能再补录或删除事件。")) return;
      try {
        await api(`/api/disease/cases/${selectedId}/freeze`, { method: "POST" });
        await load(selectedId);
        toast("案件已冻结，快照已保存");
      } catch (err) { toast(err.message, true); }
    });

    $("#btn-case-delete").addEventListener("click", async () => {
      if (!sel()) return;
      if (!confirm(`删除案件「${sel().disease_name}」及其全部事件与快照？此操作不可恢复。`)) return;
      try {
        await api(`/api/disease/cases/${selectedId}`, { method: "DELETE" });
        selectedId = null;
        await load();
        toast("案件已删除");
      } catch (err) { toast(err.message, true); }
    });
  }

  // ------------------------------------------------------------ 工具表单

  function bindToolForm() {
    $("#tool-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/api/disease/tools", {
          method: "POST",
          body: { name: f.elements.name.value.trim(), note: f.elements.note.value.trim() },
        });
        f.reset();
        await load(selectedId);
        toast("工具已添加");
      } catch (err) { toast(err.message, true); }
    });
  }

  // ------------------------------------------------------------ 事件增删改

  function bindEventForm() {
    $("#event-type").addEventListener("change", syncEventTypeFields);
    $("#btn-event-now").addEventListener("click", () => {
      $("#event-form").elements.ts.value = nowLocalInput();
    });

    $("#event-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!sel()) { toast("请先选择案件", true); return; }
      if (frozen()) { toast("案件已冻结，不能登记事件", true); return; }
      const f = e.target;
      const type = f.elements.type.value;
      const body = {
        type,
        ts: f.elements.ts.value,
        tool_id: f.elements.tool_id.value || null,
        zone_id: f.elements.zone_id.value || null,
        qualified: f.elements.qualified.checked,
        note: f.elements.note.value.trim(),
      };
      const id = f.elements.id.value;
      try {
        if (id) {
          await api(`/api/disease/events/${id}`, { method: "PUT", body });
          toast("事件已修改，结果已按时间重算");
        } else {
          await api(`/api/disease/cases/${selectedId}/events`, { method: "POST", body });
          toast("事件已登记，结果已重算");
        }
        resetEventForm();
        await load(selectedId);
      } catch (err) { toast(err.message, true); }
    });

    $("#event-cancel").addEventListener("click", resetEventForm);
  }

  function editEvent(evRow) {
    if (frozen()) return;
    const f = $("#event-form");
    f.elements.id.value = evRow.id;
    f.elements.type.value = evRow.type;
    f.elements.ts.value = evRow.ts.replace(" ", "T");
    if (evRow.tool_id) f.elements.tool_id.value = evRow.tool_id;
    if (evRow.zone_id) f.elements.zone_id.value = evRow.zone_id;
    f.elements.qualified.checked = !!evRow.qualified;
    f.elements.note.value = evRow.note || "";
    syncEventTypeFields();
    $("#event-submit").textContent = "保存修改";
    $("#event-cancel").classList.remove("hidden");
    f.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function removeEvent(evRow) {
    if (frozen()) return;
    if (!confirm(`删除 ${TYPE_TEXT[evRow.type]} 事件（${fmtTs(evRow.ts)}）？删除后将按时间整体重算。`)) return;
    try {
      await api(`/api/disease/events/${evRow.id}`, { method: "DELETE" });
      if (Number($("#event-form").elements.id.value) === evRow.id) resetEventForm();
      await load(selectedId);
      toast("事件已删除，结果已重算");
    } catch (err) { toast(err.message, true); }
  }

  // ------------------------------------------------------------ 筛选 / 导出

  function bindFilters() {
    $("#filter-type").addEventListener("change", (e) => { filter.type = e.target.value; renderZoneTable(); renderEvents(); });
    $("#filter-tool").addEventListener("change", (e) => { filter.tool_id = e.target.value; renderEvents(); });
    $("#filter-zone").addEventListener("change", (e) => { filter.zone_id = e.target.value; renderZoneTable(); renderEvents(); });
    $("#filter-quarantine").addEventListener("change", (e) => {
      filter.quarantineOnly = e.target.checked; renderZoneTable();
    });
    $("#btn-export").addEventListener("click", () => {
      if (!selectedId) { toast("请先选择案件", true); return; }
      window.location.href = `/api/disease/export?case_id=${selectedId}`;
    });
  }

  // ------------------------------------------------------------ 启动

  resetEventForm();
  bindCaseForms();
  bindToolForm();
  bindEventForm();
  bindFilters();
  load().catch((err) => toast(err.message, true));
})();
