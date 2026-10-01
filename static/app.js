/* 温室分区轮灌编排台 —— 原生 JS */
(() => {
  "use strict";

  const SLOT_MIN = 15;
  const SLOTS = 96;

  let state = null;
  let selectedZoneId = null;
  let editingValveId = null;
  let editingBlackoutId = null;
  // 修订冲突草稿：{ baseItems（共同基线时段）, ancestorRevision, localItems,
  //                baseline（基线约束快照） }
  let draft = null;
  let cleanSnapshot = null;   // 最近一次干净服务器状态的约束快照
  let mergeChoices = {};      // 复核弹窗逐项选择：zoneId -> 'local' | 'server'
  const $ = (sel, root = document) => root.querySelector(sel);

  // ------------------------------------------------------------ 工具

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.className = "toast"), 3200);
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

  function hhmm(slot) {
    slot = Math.max(0, Math.min(SLOTS, Math.round(slot)));
    const h = Math.floor(slot / 4);
    const m = (slot % 4) * 15;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  }

  function toSlot(t) {
    if (!t) return null;
    const [h, m] = t.split(":").map(Number);
    return h * 4 + m / 15;
  }

  function slotsForWater(waterL, flowLmin) {
    return Math.max(1, Math.ceil(waterL / (flowLmin * SLOT_MIN)));
  }

  // ------------------------------------------------------------ 修订 / 草稿状态

  function inDraft() { return draft !== null; }

  function snapshotOf(st) {
    // 与计划有效性相关的约束快照：供冲突时识别「约束也变了」
    return {
      revision: st.revision,
      total_flow: st.settings.total_flow,
      valves: st.valves.map((v) => [v.id, v.name, v.flow_lmin, v.concur]),
      zones: st.zones.map((z) => [z.id, z.name, z.water_l, z.valve_id,
                                  z.earliest_slot, z.latest_slot]),
      blackouts: st.blackouts.map((b) => [b.id, b.name, b.start_slot, b.end_slot]),
    };
  }

  function adoptServer(st) {
    // state 始终保存服务器最新状态（state.items 即服务器时段）；
    // 草稿模式下页面渲染的本地时段经 currentItems() 叠加，不污染服务器数据
    if (!inDraft()) cleanSnapshot = snapshotOf(st);
    state = st;
  }

  function currentItems() {
    // 页面当前应展示 / 编辑的时段：有本地草稿时用草稿，否则用服务器版本
    return inDraft() ? draft.localItems : state.items;
  }

  async function refresh() {
    adoptServer(await api("/api/state"));
    renderAllFromState();
  }

  function itemMap(items) {
    const m = new Map();
    (items || []).forEach((it) => m.set(it.zone_id, it));
    return m;
  }

  function sameItem(a, b) {
    if (!a && !b) return true;    // 两边都未编排视为相同
    if (!a || !b) return false;
    return a.start_slot === b.start_slot && a.length === b.length;
  }

  async function submitSchedule(items, baseItems) {
    // 携带页面基线修订做 CAS；成功则进入服务器版本，409 则保留本地草稿
    const baseRevision = state ? state.revision : null;
    try {
      const next = await api("/api/schedule", {
        method: "PUT",
        body: { items, base_revision: baseRevision },
      });
      draft = null;
      mergeChoices = {};
      adoptServer(next);
      renderAllFromState();
      return { ok: true, state: next };
    } catch (e) {
      if (e.status === 409 && e.data && e.data.conflict === "revision") {
        // enterConflict 内部已渲染并打开复核弹窗
        enterConflict(e.data.state, items, baseItems);
        return { ok: false, conflict: true };
      }
      throw e;
    }
  }

  function enterConflict(serverState, localItems, baseItems) {
    // 记录三方对比所需的共同基线（首次冲突时冻结，重试合并期间不再前移）
    if (!inDraft()) {
      draft = {
        ancestorRevision: state ? state.revision : null,
        baseline: cleanSnapshot,
        baseItems: baseItems || (state ? state.items : []),
        localItems: localItems,
      };
    } else {
      // 合并重试：刚组装的合并稿即新的本地草稿（基线项保持不动，三方语义不变）
      draft.localItems = localItems;
    }
    adoptServer(serverState);
    renderAllFromState();
    openMergeDialog();
  }

  // ------------------------------------------------------------ 设置

  function renderSettings() {
    $("#plan-date").value = state.settings.plan_date || "";
    $("#total-flow").value = state.settings.total_flow;
    $("#status-meta").dataset.revision = state.revision;
  }

  async function saveSettings(patch) {
    try {
      // 总流量变更会使服务端递增修订：接受新状态后本地基线即最新
      adoptServer(await api("/api/settings", { method: "POST", body: patch }));
      renderSettings();
      renderTimeline();
      renderIssues();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 阀门

  const valveColorIndex = (vid) => {
    const i = state.valves.findIndex((v) => v.id === vid);
    return i < 0 ? "other" : i;
  };

  function renderValves() {
    const ul = $("#valve-list");
    ul.innerHTML = "";
    state.valves.forEach((v) => {
      const li = document.createElement("li");
      if (editingValveId === v.id) {
        li.appendChild(buildValveEditor(v));
      } else {
        li.className = "entity-row";
        li.innerHTML =
          `<span class="grow"><span class="name"><i class="swatch valve-${valveColorIndex(v.id)}"></i>${escapeHtml(v.name)}</span>
             <div class="meta">${v.flow_lmin} L/min · 并发上限 ${v.concur}</div></span>`;
        const edit = iconButton("✏️", "编辑", () => { editingValveId = v.id; renderValves(); });
        const del = iconButton("🗑", "删除", () => removeValve(v), true);
        li.append(edit, del);
      }
      ul.appendChild(li);
    });

    const sel = $("#zone-form select[name=valve_id]");
    sel.innerHTML = state.valves.length
      ? state.valves.map((v) => `<option value="${v.id}">${escapeHtml(v.name)}（${v.flow_lmin} L/min）</option>`).join("")
      : `<option value="">（请先添加阀门）</option>`;
  }

  function buildValveEditor(v) {
    const wrap = document.createElement("div");
    wrap.className = "inline-form";
    wrap.style.gridTemplateColumns = "1fr 64px 52px auto auto";
    wrap.innerHTML =
      `<input data-k="name" value="${escapeAttr(v.name)}">
       <input data-k="flow_lmin" type="number" min="0.1" step="0.1" value="${v.flow_lmin}">
       <input data-k="concur" type="number" min="1" step="1" value="${v.concur}">`;
    const saveBtn = btn("保存", async () => {
      const body = readEditor(wrap, ["name", "flow_lmin", "concur"]);
      body.flow_lmin = Number(body.flow_lmin);
      body.concur = Number(body.concur);
      try {
        adoptServer(await api(`/api/valves/${v.id}`, { method: "PUT", body }));
        editingValveId = null;
        toast("阀门已更新，计划已重新校验");
        renderValves(); renderZones(); renderTimeline(); renderIssues();
      } catch (e) { toast(e.message, true); }
    }, true);
    const cancelBtn = btn("取消", () => { editingValveId = null; renderValves(); });
    wrap.append(saveBtn, cancelBtn);
    return wrap;
  }

  async function removeValve(v) {
    if (!confirm(`确定删除阀门「${v.name}」？`)) return;
    try {
      adoptServer(await api(`/api/valves/${v.id}`, { method: "DELETE" }));
      toast("阀门已删除");
      renderValves(); renderZones(); renderTimeline(); renderIssues(); syncZoneForm();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 分区

  function renderZones() {
    const ul = $("#zone-list");
    ul.innerHTML = "";
    const valveOrder = new Map(state.valves.map((v, i) => [v.id, i]));
    const sorted = [...state.zones].sort((a, b) =>
      (valveOrder.get(a.valve_id) ?? 99) - (valveOrder.get(b.valve_id) ?? 99) || a.id - b.id);

    sorted.forEach((z) => {
      const item = currentItems().find((it) => it.zone_id === z.id);
      const issue = state.evaluation.issues.find((i) => i.zone_id === z.id);
      const unsched = state.evaluation.unscheduled.find((u) => u.zone_id === z.id);
      const li = document.createElement("li");
      li.className = "entity-row clickable" + (z.id === selectedZoneId ? " selected" : "");
      const when = item
        ? `${hhmm(item.start_slot)}–${hhmm(item.start_slot + item.length)}`
        : "未编排";
      const mark = issue || unsched ? ` <span title="${escapeAttr((issue ? issue.reasons : unsched.reasons).join("；"))}">🔴</span>` : "";
      li.innerHTML =
        `<span class="grow"><span class="name"><i class="swatch valve-${valveColorIndex(z.valve_id)}"></i>${escapeHtml(z.name)}${mark}</span>
           <div class="meta">${z.water_l} L · ${escapeHtml(z.valve_name)} · ${z.earliest}–${z.latest}</div>
           <div class="meta">计划：${when}</div></span>`;
      li.addEventListener("click", () => selectZone(z.id));
      const del = iconButton("🗑", "删除分区", async (e) => {
        e.stopPropagation();
        if (!confirm(`确定删除分区「${z.name}」及其计划？`)) return;
        try {
          adoptServer(await api(`/api/zones/${z.id}`, { method: "DELETE" }));
          if (selectedZoneId === z.id) { selectedZoneId = null; resetZoneForm(); }
          toast("分区已删除");
          renderAllFromState();
        } catch (err) { toast(err.message, true); }
      }, true);
      li.appendChild(del);
      ul.appendChild(li);
    });
  }

  function selectZone(id) {
    selectedZoneId = id;
    renderZones();
    syncZoneForm();
    const block = $(`.block[data-zone-id="${id}"]`);
    if (block) block.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function resetZoneForm() {
    const form = $("#zone-form");
    form.reset();
    form.elements.id.value = "";
    $("#zone-submit").textContent = "添加分区";
    $("#item-start-label").classList.add("hidden");
  }

  function syncZoneForm() {
    const form = $("#zone-form");
    const z = selectedZoneId ? state.zones.find((x) => x.id === selectedZoneId) : null;
    if (!z) { resetZoneForm(); return; }
    form.elements.id.value = z.id;
    form.elements.name.value = z.name;
    form.elements.water_l.value = z.water_l;
    form.elements.valve_id.value = z.valve_id ?? "";
    form.elements.earliest.value = z.earliest;
    form.elements.latest.value = z.latest;
    $("#zone-submit").textContent = "保存分区修改";
    const item = currentItems().find((it) => it.zone_id === z.id);
    const row = $("#item-start-label");
    row.classList.remove("hidden");
    form.elements.item_start.value = item ? hhmm(item.start_slot) : "";
  }

  // ------------------------------------------------------------ 停水时段

  function renderBlackouts() {
    const ul = $("#blackout-list");
    ul.innerHTML = "";
    state.blackouts.forEach((b) => {
      const li = document.createElement("li");
      if (editingBlackoutId === b.id) {
        li.appendChild(buildBlackoutEditor(b));
      } else {
        li.className = "entity-row";
        li.innerHTML =
          `<span class="grow"><span class="name">${escapeHtml(b.name)}</span>
             <div class="meta">${b.start} – ${b.end}</div></span>`;
        li.append(
          iconButton("✏️", "编辑", () => { editingBlackoutId = b.id; renderBlackouts(); }),
          iconButton("🗑", "删除", async () => {
            if (!confirm(`删除停水时段「${b.name}」？`)) return;
            try {
              adoptServer(await api(`/api/blackouts/${b.id}`, { method: "DELETE" }));
              toast("停水时段已删除，计划已重新校验");
              renderAllFromState();
            } catch (e) { toast(e.message, true); }
          }, true));
      }
      ul.appendChild(li);
    });
  }

  function buildBlackoutEditor(b) {
    const wrap = document.createElement("div");
    wrap.className = "inline-form";
    wrap.style.gridTemplateColumns = "1fr 70px 70px auto auto";
    wrap.innerHTML =
      `<input data-k="name" value="${escapeAttr(b.name)}">
       <input data-k="start" type="time" step="900" value="${b.start}">
       <input data-k="end" type="time" step="900" value="${b.end}">`;
    wrap.append(
      btn("保存", async () => {
        const body = readEditor(wrap, ["name", "start", "end"]);
        try {
          adoptServer(await api(`/api/blackouts/${b.id}`, { method: "PUT", body }));
          editingBlackoutId = null;
          toast("停水时段已更新，计划已重新校验");
          renderAllFromState();
        } catch (e) { toast(e.message, true); }
      }, true),
      btn("取消", () => { editingBlackoutId = null; renderBlackouts(); }));
    return wrap;
  }

  // ------------------------------------------------------------ 时间轴

  function renderTimeline() {
    renderRuler();
    const tracks = $("#tracks");
    tracks.innerHTML = "";
    tracks.appendChild(buildBlackoutTrack());

    const valveOrder = new Map(state.valves.map((v, i) => [v.id, i]));
    const sorted = [...state.zones].sort((a, b) =>
      (valveOrder.get(a.valve_id) ?? 99) - (valveOrder.get(b.valve_id) ?? 99) || a.id - b.id);

    if (!sorted.length) {
      const empty = document.createElement("div");
      empty.className = "track";
      empty.innerHTML = `<span class="unscheduled-row" style="cursor:default">暂无分区，请在左侧录入（或点击「恢复示例」）</span>`;
      tracks.appendChild(empty);
      return;
    }

    sorted.forEach((z) => tracks.appendChild(buildZoneTrack(z)));
  }

  function renderRuler() {
    const ruler = $("#ruler-track");
    ruler.innerHTML = "";
    for (let s = 0; s < SLOTS; s++) {
      if (s % 4 !== 0) continue;
      const tick = document.createElement("div");
      tick.className = "tick major";
      tick.style.left = pct(s);
      const label = document.createElement("span");
      label.className = "tick-label";
      label.textContent = hhmm(s);
      tick.appendChild(label);
      ruler.appendChild(tick);
    }
  }

  function buildBlackoutTrack() {
    const row = document.createElement("div");
    row.className = "track";
    row.style.height = "20px";
    row.style.setProperty("--track-h", "20px");
    const label = document.createElement("span");
    label.className = "label-absolute";
    label.textContent = "停水";
    row.appendChild(label);
    state.blackouts.forEach((b) => {
      const band = document.createElement("div");
      band.className = "blackout-band";
      setSlotRange(band, b.start_slot, b.end_slot);
      band.innerHTML = `<span class="bo-label">${escapeHtml(b.name)}</span>`;
      band.title = `${b.name} ${b.start}–${b.end}`;
      row.appendChild(band);
    });
    return row;
  }

  function buildZoneTrack(z) {
    const track = document.createElement("div");
    track.className = "track";
    const issue = state.evaluation.issues.find((i) => i.zone_id === z.id);
    const unsched = state.evaluation.unscheduled.find((u) => u.zone_id === z.id);
    if (issue || unsched) track.classList.add("has-conflict");

    const label = document.createElement("span");
    label.className = "label-absolute";
    label.textContent = z.name;
    label.title = `${z.name}（${z.valve_name}）`;
    track.appendChild(label);

    // 允许灌溉时段
    const win = document.createElement("div");
    win.className = "window-band";
    setSlotRange(win, z.earliest_slot, z.latest_slot);
    win.title = `允许时段 ${z.earliest}–${z.latest}`;
    track.appendChild(win);

    // 停水覆盖
    state.blackouts.forEach((b) => {
      const s = Math.max(b.start_slot, 0);
      const e = Math.min(b.end_slot, SLOTS);
      if (e > s) {
        const band = document.createElement("div");
        band.className = "blackout-band";
        setSlotRange(band, s, e);
        track.appendChild(band);
      }
    });

    const item = currentItems().find((it) => it.zone_id === z.id);
    if (item) {
      track.appendChild(buildBlock(z, item, issue));
    } else {
      const note = document.createElement("div");
      note.className = "unscheduled-row";
      note.textContent = `⛔ 未编排 —— ${(unsched ? unsched.reasons : ["尚未安排"]).join("；")}`;
      note.title = note.textContent;
      note.addEventListener("click", () => selectZone(z.id));
      track.appendChild(note);
    }
    return track;
  }

  function buildBlock(z, item, issue) {
    const block = document.createElement("div");
    const colorIdx = valveColorIndex(z.valve_id);
    block.className = `block valve-${colorIdx}` + (issue ? " conflict" : "") +
      (z.id === selectedZoneId ? " selected" : "") + (inDraft() ? " draft-item" : "");
    block.dataset.zoneId = z.id;
    setSlotRange(block, item.start_slot, item.start_slot + item.length);
    block.innerHTML =
      `<div class="b-name">${escapeHtml(z.name)}</div><div class="b-time">${hhmm(item.start_slot)}–${hhmm(item.start_slot + item.length)}</div>`;
    const reasons = issue ? "\n冲突：" + issue.reasons.join("；") : "";
    block.title = `${z.name}（${z.valve_name} ${z.flow_lmin} L/min）\n` +
      `${hhmm(item.start_slot)}–${hhmm(item.start_slot + item.length)}，` +
      `${item.length * SLOT_MIN} 分钟，需水 ${z.water_l} L${reasons}`;
    block.addEventListener("click", () => selectZone(z.id));
    enableDrag(block, z, item);
    return block;
  }

  // ------------------------------------------------------------ 拖拽

  function enableDrag(block, zone, item) {
    let startX = 0, originSlot = 0, moved = false, ghost = null, track = null, colW = 0;

    block.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      track = block.parentElement;
      const rect = track.getBoundingClientRect();
      colW = rect.width / SLOTS;
      startX = ev.clientX;
      originSlot = item.start_slot;
      moved = false;
      block.setPointerCapture(ev.pointerId);

      const onMove = (e) => {
        const dx = e.clientX - startX;
        if (!moved && Math.abs(dx) < 5) return;
        if (!moved) {
          moved = true;
          block.classList.add("dragging");
          ghost = document.createElement("div");
          ghost.className = "ghost-hint";
          ghost.style.width = (item.length * colW) + "px";
          track.appendChild(ghost);
        }
        let slot = Math.round(originSlot + dx / colW);
        slot = Math.max(0, Math.min(SLOTS - item.length, slot));
        ghost.style.left = (slot * colW) + "px";
        ghost.innerHTML = `<span class="tip">${hhmm(slot)}–${hhmm(slot + item.length)}</span>`;
      };

      const onUp = async (e) => {
        block.removeEventListener("pointermove", onMove);
        block.removeEventListener("pointerup", onUp);
        block.classList.remove("dragging");
        if (ghost) ghost.remove();
        if (!moved) return; // 视为点击
        const dx = e.clientX - startX;
        let slot = Math.max(0, Math.min(SLOTS - item.length,
          Math.round(originSlot + dx / colW)));
        if (slot === originSlot) return;
        await moveItem(zone.id, slot);
      };

      block.addEventListener("pointermove", onMove);
      block.addEventListener("pointerup", onUp);
    });
  }

  async function moveItem(zoneId, newStart) {
    const baseItems = inDraft() ? draft.localItems : state.items;
    const items = baseItems.map((it) =>
      it.zone_id === zoneId ? { ...it, start_slot: newStart } : it);
    try {
      const res = await submitSchedule(items, baseItems);
      if (!res.ok) {
        toast("计划修订已过期：本地草稿已保留，请逐项复核差异后再提交", true);
        return;
      }
      const issue = res.state.evaluation.issues.find((i) => i.zone_id === zoneId);
      toast(issue ? `已移动：${issue.reasons.join("；")}` : "时段已调整，校验通过",
        Boolean(issue));
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 冲突面板 / 状态

  function renderIssues() {
    const ev = state.evaluation;
    const bar = $("#status-bar");
    const text = $("#status-text");
    const meta = $("#status-meta");
    if (inDraft()) {
      meta.textContent = `服务器修订 ${state.revision}（本地草稿基于修订 ${draft.ancestorRevision}）` +
        ` · 峰值流量 ${ev.max_flow} / ${state.settings.total_flow} L/min · ${state.zones.length} 个分区`;
    } else {
      meta.textContent = `修订 ${state.revision} · 峰值流量 ${ev.max_flow} / ${state.settings.total_flow} L/min · ${state.zones.length} 个分区`;
    }

    const panel = $("#issues-panel");
    const list = $("#issues-list");
    list.innerHTML = "";

    ev.issues.forEach((i) => {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(i.zone_name)}</strong>
        <span class="where">${hhmm(i.start_slot)}–${hhmm(i.end_slot)}</span>：
        <span class="reason">${i.reasons.map(escapeHtml).join("；")}</span>`;
      list.appendChild(li);
    });
    ev.unscheduled.forEach((u) => {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(u.zone_name)}</strong>
        <span class="where">未编排</span>：
        <span class="reason">${u.reasons.map(escapeHtml).join("；")}</span>`;
      li.addEventListener("click", () => selectZone(u.zone_id));
      li.style.cursor = "pointer";
      list.appendChild(li);
    });

    if (inDraft()) {
      // 草稿模式：展示的是本地时段，但 evaluation 基于服务器约束；
      // 不做有效/无效结论，仅提示操作员先完成复核
      bar.className = "status-bar draft";
      text.textContent = "📝 正在浏览本地草稿（未提交）——请通过「逐项复核并提交」合并后再查看最终校验";
      panel.classList.add("hidden");
    } else if (ev.valid) {
      bar.className = "status-bar";
      text.textContent = `✅ 计划有效（修订 ${state.revision}）：${state.items.length} 个分区已编排，无冲突`;
      panel.classList.add("hidden");
    } else {
      bar.className = "status-bar bad";
      const n = ev.issues.length + ev.unscheduled.length;
      text.textContent = `❌ 校验未通过（修订 ${state.revision}）：${n} 个分区存在冲突或未编排`;
      panel.classList.remove("hidden");
    }
    renderDraftBanner();
  }

  function renderDraftBanner() {
    const banner = $("#draft-banner");
    if (!inDraft()) {
      banner.classList.add("hidden");
      return;
    }
    $("#draft-base-rev").textContent = draft.ancestorRevision;
    $("#draft-server-rev").textContent = state.revision;
    banner.classList.remove("hidden");
  }

  // ------------------------------------------------------------ 表单提交

  function bindForms() {
    $("#valve-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        name: f.elements.name.value.trim(),
        flow_lmin: Number(f.elements.flow_lmin.value),
        concur: Number(f.elements.concur.value),
      };
      try {
        await api("/api/valves", { method: "POST", body });
        f.reset(); f.elements.concur.value = 1;
        adoptServer(await api("/api/state"));
        renderAllFromState();
        toast("阀门已添加");
      } catch (err) { toast(err.message, true); }
    });

    $("#blackout-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        name: f.elements.name.value.trim() || "停水时段",
        start: f.elements.start.value,
        end: f.elements.end.value,
      };
      try {
        await api("/api/blackouts", { method: "POST", body });
        f.reset();
        adoptServer(await api("/api/state"));
        renderAllFromState();
        toast("停水时段已添加，计划已重新校验");
      } catch (err) { toast(err.message, true); }
    });

    $("#zone-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const id = f.elements.id.value;
      const body = {
        name: f.elements.name.value.trim(),
        water_l: Number(f.elements.water_l.value),
        valve_id: Number(f.elements.valve_id.value),
        earliest: f.elements.earliest.value,
        latest: f.elements.latest.value,
      };
      const wantStart = toSlot(f.elements.item_start.value);
      try {
        if (id) {
          // 分区约束 PUT 可能已递增修订：先接受最新状态，再以其为基线提交时段
          adoptServer(await api(`/api/zones/${id}`, { method: "PUT", body }));
          // 同步该分区的计划时段：按新水量/流量重算时长；
          // 草稿模式下在本地草稿上改，不触碰其他窗口已提交的服务器时段
          const baseItems = currentItems();
          const z2 = state.zones.find((x) => x.id === Number(id));
          const length = slotsForWater(z2.water_l, z2.flow_lmin);
          const old = baseItems.find((it) => it.zone_id === z2.id);
          const items = baseItems.filter((it) => it.zone_id !== z2.id);
          if (wantStart !== null || old) {
            let start = wantStart !== null ? wantStart : old.start_slot;
            start = Math.max(0, Math.min(SLOTS - length, start));
            items.push({ zone_id: z2.id, start_slot: start, length });
          }
          const res = await submitSchedule(items, baseItems);
          if (!res.ok) {
            toast("分区已保存，但计划修订已过期：时段草稿已保留，请逐项复核", true);
          } else {
            toast("分区已保存，计划已重新校验");
          }
        } else {
          const created = await api("/api/zones", { method: "POST", body });
          selectedZoneId = created.id;
          adoptServer(await api("/api/state"));
          resetZoneForm();
          toast("分区已添加，可点击「自动编排」或在时间轴上拖动");
        }
        renderAllFromState();
      } catch (err) { toast(err.message, true); }
    });

    $("#zone-cancel").addEventListener("click", () => {
      selectedZoneId = null;
      resetZoneForm();
      renderZones();
    });
  }

  // ------------------------------------------------------------ 顶部按钮

  function bindActions() {
    $("#plan-date").addEventListener("change", (e) => saveSettings({ plan_date: e.target.value }));
    $("#total-flow").addEventListener("change", (e) => saveSettings({ total_flow: e.target.value }));

    $("#btn-generate").addEventListener("click", async () => {
      if (inDraft()) { openMergeDialog(); return; }
      try {
        const baseItems = state.items;
        const next = await api("/api/generate", {
          method: "POST",
          body: { base_revision: state.revision },
        }).catch((e) => {
          if (e.status === 409 && e.data && e.data.conflict === "revision") {
            // 编排未在服务端生效：把编排结果作为本地草稿进入复核
            const gen = schedulerLocalGenerate();
            enterConflict(e.data.state, gen, baseItems);
            toast("计划修订已过期：编排结果已保留为本地草稿，请逐项复核", true);
            return "__conflict__";
          }
          throw e;
        });
        if (next === "__conflict__") return;
        draft = null; mergeChoices = {}; adoptServer(next);
        const n = next.evaluation.unscheduled.length;
        if (n === 0 && next.evaluation.valid) toast("自动编排完成，全部满足约束 ✅");
        else if (n > 0) toast(`已编排其他分区，${n} 个分区无可行位置`, true);
        else toast("编排完成，但存在约束冲突，请查看时间轴", true);
        renderAllFromState();
      } catch (e) { toast(e.message, true); }
    });

    $("#btn-save").addEventListener("click", async () => {
      if (inDraft()) { openMergeDialog(); return; }
      const items = state.items.map(
        ({ zone_id, start_slot, length }) => ({ zone_id, start_slot, length }));
      try {
        const res = await submitSchedule(items, items);
        if (!res.ok) {
          toast("计划修订已过期：本地草稿已保留，请逐项复核差异后再提交", true);
          return;
        }
        const ev = res.state.evaluation;
        if (ev.valid) toast(`计划已保存（修订 ${res.state.revision}），校验通过 ✅`);
        else toast(`已保存（修订 ${res.state.revision}），但有 ${ev.issues.length + ev.unscheduled.length} 处问题待处理`, true);
      } catch (e) { toast(e.message, true); }
    });

    $("#btn-clear").addEventListener("click", async () => {
      if (!confirm("将清空所有阀门、分区、停水时段与计划，确定继续？")) return;
      try {
        draft = null; mergeChoices = {};
        adoptServer(await api("/api/reset", { method: "POST", body: { sample: false } }));
        selectedZoneId = null;
        resetZoneForm();
        renderAllFromState();
        toast("已清空，可从零开始编排");
      } catch (e) { toast(e.message, true); }
    });

    $("#btn-sample").addEventListener("click", async () => {
      if (!confirm("将清空当前数据并恢复内置示例，确定继续？")) return;
      try {
        draft = null; mergeChoices = {};
        adoptServer(await api("/api/reset", { method: "POST", body: { sample: true } }));
        selectedZoneId = null;
        resetZoneForm();
        renderAllFromState();
        toast("示例数据已恢复，点击「自动编排」生成计划");
      } catch (e) { toast(e.message, true); }
    });
  }

  // ------------------------------------------------------------ 本地贪心（generate 被 409 时保留编排草稿）

  function localGenerate(st) {
    // 与服务端 scheduler.generate 同一规则：紧迫度升序、首零冲突位置
    const zones = st.zones, valves = st.valves, blackouts = st.blackouts;
    const total = st.settings.total_flow;
    const valveById = new Map(valves.map((v) => [v.id, v]));
    const bo = new Set();
    blackouts.forEach((b) => {
      for (let s = Math.max(0, b.start_slot); s < Math.min(SLOTS, b.end_slot); s++) bo.add(s);
    });
    const flowUsed = new Array(SLOTS).fill(0);
    const valveUsed = new Map(valves.map((v) => [v.id, new Array(SLOTS).fill(0)]));
    const prepared = zones.map((z) => {
      const v = valveById.get(z.valve_id);
      return [z, v ? slotsForWater(z.water_l, v.flow_lmin) : 0];
    });
    prepared.sort((a, b) =>
      ((b[0].latest_slot - b[0].earliest_slot) - b[1]) -
      ((a[0].latest_slot - a[0].earliest_slot) - a[1]) ||
      b[0].water_l - a[0].water_l || a[0].id - b[0].id);
    const items = [];
    for (const [z, length] of prepared) {
      const v = valveById.get(z.valve_id);
      if (!v) continue;
      for (let start = z.earliest_slot; start <= z.latest_slot - length; start++) {
        let badRun = false;
        for (let s = start; s < start + length; s++) {
          if (bo.has(s) || flowUsed[s] + z.flow_lmin > total ||
              valveUsed.get(z.valve_id)[s] + 1 > v.concur) { badRun = true; break; }
        }
        if (badRun) continue;
        items.push({ zone_id: z.id, start_slot: start, length });
        for (let s = start; s < start + length; s++) {
          flowUsed[s] += z.flow_lmin;
          valveUsed.get(z.valve_id)[s] += 1;
        }
        break;
      }
    }
    items.sort((a, b) => a.start_slot - b.start_slot || a.zone_id - b.zone_id);
    return items;
  }
  function schedulerLocalGenerate() { return localGenerate(state); }

  // ------------------------------------------------------------ 三方差异 / 复核弹窗

  function slotText(it) {
    if (!it) return "— 未编排 —";
    return `${hhmm(it.start_slot)}–${hhmm(it.start_slot + it.length)}（${it.length * SLOT_MIN} 分钟）`;
  }

  function describeConstraintChanges(base, server) {
    if (!base) return [];
    const notes = [];
    if (base.total_flow !== server.settings.total_flow) {
      notes.push(`总流量上限 ${base.total_flow} → ${server.settings.total_flow} L/min`);
    }
    const bv = new Map(base.valves.map((v) => [v[0], v]));
    const sv = new Map(server.valves.map((v) => [v.id, v]));
    for (const [id, v] of sv) {
      const old = bv.get(id);
      if (!old) { notes.push(`新增阀门「${v.name}」（${v.flow_lmin} L/min，并发 ${v.concur}）`); }
      else if (old[2] !== v.flow_lmin) {
        notes.push(`阀门「${v.name}」额定流量 ${old[2]} → ${v.flow_lmin} L/min`);
      } else if (old[3] !== v.concur) {
        notes.push(`阀门「${v.name}」并发上限 ${old[3]} → ${v.concur}`);
      }
    }
    for (const [id, old] of bv) if (!sv.has(id)) notes.push(`阀门「${old[1]}」已删除`);

    const bz = new Map(base.zones.map((z) => [z[0], z]));
    const sz = new Map(server.zones.map((z) => [z.id, z]));
    for (const [id, z] of sz) {
      const old = bz.get(id);
      if (!old) { notes.push(`新增分区「${z.name}」`); continue; }
      if (old[2] !== z.water_l) notes.push(`分区「${z.name}」水量 ${old[2]} → ${z.water_l} L`);
      if (old[3] !== z.valve_id) notes.push(`分区「${z.name}」改绑阀门`);
      if (old[4] !== z.earliest_slot || old[5] !== z.latest_slot) {
        notes.push(`分区「${z.name}」允许时段 ${hhmm(old[4])}–${hhmm(old[5])} → ${z.earliest}–${z.latest}`);
      }
    }
    for (const [id, old] of bz) if (!sz.has(id)) notes.push(`分区「${old[1]}」已删除`);

    const bb = new Map(base.blackouts.map((b) => [b[0], b]));
    const sb = new Map(server.blackouts.map((b) => [b.id, b]));
    for (const [id, b] of sb) {
      const old = bb.get(id);
      if (!old) notes.push(`新增停水时段「${b.name}」${b.start}–${b.end}`);
      else if (old[2] !== b.start_slot || old[3] !== b.end_slot) {
        notes.push(`停水时段「${b.name}」改为 ${b.start}–${b.end}`);
      }
    }
    for (const [id, old] of bb) if (!sb.has(id)) notes.push(`停水时段「${old[1]}」已删除`);
    return notes;
  }

  function buildMergeRows() {
    // 三方：baseItems=共同基线，draft.localItems=本地，state.items=服务器最新
    const zonesById = new Map(state.zones.map((z) => [z.id, z]));
    const baseM = itemMap(draft.baseItems);
    const localM = itemMap(draft.localItems);
    const serverM = itemMap(state.items);
    const ids = new Set([...baseM.keys(), ...localM.keys(), ...serverM.keys()]);
    const rows = [];
    for (const zid of ids) {
      const b = baseM.get(zid) || null;
      const l = localM.get(zid) || null;
      const s = serverM.get(zid) || null;
      const zoneExists = zonesById.has(zid);
      let kind, forced = null;
      if (sameItem(l, s)) continue;  // 两边一致（含都未编排）：无需选择
      const lChanged = !sameItem(b, l);
      const sChanged = !sameItem(b, s);
      if (lChanged && sChanged) kind = "both";
      else if (lChanged) kind = "local";
      else kind = "server";
      // 分区已在服务器删除：任何含该分区的本地时段都无法提交
      if (!zoneExists && l) forced = "drop";
      rows.push({ zoneId: zid, zone: zonesById.get(zid) || null,
                  kind, base: b, local: l, server: s, forced });
    }
    rows.sort((a, b) => {
      const sa = (a.local || a.server || a.base || {}).start_slot ?? 99;
      const sb = (b.local || b.server || b.base || {}).start_slot ?? 99;
      return sa - sb || a.zoneId - b.zoneId;
    });
    return rows;
  }

  function defaultChoice(row) {
    if (row.forced === "drop") return "server";
    if (row.kind === "local") return "local";    // 仅本地改：默认保留本地
    if (row.kind === "server") return "server";  // 仅服务器改：默认采用对方
    return "";                                   // 双方都改：必须人工二选一
  }

  const KIND_LABEL = {
    local: ["仅本地", "kind-local"], server: ["仅服务器", "kind-server"],
    both: ["双方都改", "kind-both"],
  };

  function openMergeDialog() {
    if (!inDraft()) return;
    $("#merge-base-rev").textContent = draft.ancestorRevision;
    $("#merge-server-rev").textContent = state.revision;
    const notes = describeConstraintChanges(draft.baseline, state);
    const warn = $("#constraint-warn");
    if (notes.length) {
      $("#constraint-warn-text").textContent = notes.join("；") + "。";
      warn.classList.remove("hidden");
    } else {
      warn.classList.add("hidden");
    }
    renderMergeRows();
    $("#merge-overlay").classList.remove("hidden");
  }

  function closeMergeDialog() {
    $("#merge-overlay").classList.add("hidden");
  }

  function renderMergeRows() {
    const rows = buildMergeRows();
    const tbody = $("#merge-rows");
    tbody.innerHTML = "";
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      const name = row.zone ? row.zone.name : `分区 #${row.zoneId}（服务器已删除）`;
      const [kindLabel, kindCls] = KIND_LABEL[row.kind];
      const note = row.forced === "drop"
        ? `<div class="merge-note">该分区已被其他窗口删除，本地时段无法提交</div>` : "";
      tr.innerHTML =
        `<td class="kind">
           <div class="zone-name">${escapeHtml(name)}</div>
           <div class="zone-kind"><span class="kind-tag ${kindCls}">${kindLabel}</span></div>
           ${note}
         </td>
         <td><div class="slot-text">${slotText(row.local)}</div></td>
         <td><div class="slot-text">${slotText(row.server)}</div></td>`;
      const choiceTd = document.createElement("td");
      choiceTd.className = "cell-choice";
      if (row.forced === "drop") {
        choiceTd.innerHTML = `<span class="cell-forced">随服务器移除</span>`;
        mergeChoices[row.zoneId] = "server";
      } else {
        const stored = mergeChoices[row.zoneId];
        const choice = stored || defaultChoice(row);
        choiceTd.innerHTML =
          `<label title="保留本地时段"><input type="radio" name="mz-${row.zoneId}" value="local"` +
            (choice === "local" ? " checked" : "") + `> 本地</label>
           <label title="采用服务器时段" style="margin-left:6px"><input type="radio" name="mz-${row.zoneId}" value="server"` +
            (choice === "server" ? " checked" : "") + `> 服务器</label>`;
        choiceTd.querySelectorAll("input").forEach((inp) => {
          inp.addEventListener("change", () => {
            mergeChoices[row.zoneId] = inp.value;
            updateMergeHint();
          });
        });
      }
      tr.appendChild(choiceTd);
      tbody.appendChild(tr);
    });
    updateMergeHint();
  }

  function updateMergeHint() {
    // 「双方都改」的行必须人工二选一；其余类型有安全默认值
    const n = buildMergeRows().filter(
      (r) => r.kind === "both" && !mergeChoices[r.zoneId]).length;
    const hint = $("#merge-hint");
    if (n > 0) {
      hint.textContent = `还有 ${n} 个双方都改的分区未选择`;
      $("#merge-submit").disabled = true;
    } else {
      hint.textContent = "";
      $("#merge-submit").disabled = false;
    }
  }

  function assembleMergedItems() {
    const rows = buildMergeRows();
    const chosenByZone = new Map();
    rows.forEach((r) => {
      const ch = mergeChoices[r.zoneId] || defaultChoice(r);
      const it = ch === "local" ? r.local : r.server;
      chosenByZone.set(r.zoneId, it);
    });
    // 未出现在差异行（两边一致 / 无改动）的服务器时段原样保留
    const items = [];
    state.items.forEach((it) => {
      if (!chosenByZone.has(it.zone_id)) items.push(it);
    });
    for (const [zid, it] of chosenByZone) if (it) items.push(it);
    items.sort((a, b) => a.start_slot - b.start_slot || a.zone_id - b.zone_id);
    return items.map(({ zone_id, start_slot, length }) => ({ zone_id, start_slot, length }));
  }

  async function submitMerge() {
    const items = assembleMergedItems();
    const btn = $("#merge-submit");
    btn.disabled = true;
    try {
      const res = await submitSchedule(items, draft ? draft.baseItems : items);
      if (!res.ok) {
        // 合并期间版本又变：继续拒绝、无部分更新；用最新服务器状态重建差异，
        // 本轮选择尽量保留
        toast("合并期间计划又被更新：已继续拒绝，请按最新差异再次复核", true);
        renderMergeRows();
        return;
      }
      closeMergeDialog();
      renderAllFromState();
      toast(`合并提交成功（修订 ${res.state.revision}），请查看校验结果 ✅`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function discardDraft() {
    if (!confirm("放弃本地草稿并载入服务器最新计划？未提交的改动将丢失。")) return;
    draft = null; mergeChoices = {};
    await refresh();
    toast("已放弃草稿，载入服务器最新计划");
  }

  function bindMerge() {
    $("#btn-review-merge").addEventListener("click", openMergeDialog);
    $("#btn-discard-draft").addEventListener("click", discardDraft);
    $("#merge-close").addEventListener("click", closeMergeDialog);
    $("#merge-cancel").addEventListener("click", closeMergeDialog);
    $("#merge-submit").addEventListener("click", submitMerge);
    $("#merge-overlay").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) closeMergeDialog();
    });
  }

  // ------------------------------------------------------------ 小工具

  function renderAllFromState() {
    renderSettings();
    renderValves();
    renderZones();
    renderBlackouts();
    renderTimeline();
    renderIssues();
    syncZoneForm();
  }

  function pct(slot) { return `${(slot / SLOTS) * 100}%`; }
  function setSlotRange(el, start, end) {
    el.style.left = pct(start);
    el.style.width = `${((end - start) / SLOTS) * 100}%`;
  }

  function iconButton(txt, title, onClick, danger = false) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "icon-btn" + (danger ? " del" : "");
    b.textContent = txt;
    b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function btn(txt, onClick, primary = false) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn small ghost" + (primary ? " primary" : "");
    if (primary) { b.classList.remove("ghost"); b.style.color = "#fff"; }
    b.textContent = txt;
    b.addEventListener("click", onClick);
    return b;
  }

  function readEditor(wrap, keys) {
    const out = {};
    keys.forEach((k) => { out[k] = wrap.querySelector(`[data-k="${k}"]`).value; });
    return out;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const escapeAttr = escapeHtml;

  // ------------------------------------------------------------ 启动

  bindForms();
  bindActions();
  bindMerge();
  refresh().catch((e) => toast(e.message, true));
})();
