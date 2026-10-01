/* 肥料母液分罐 —— 原生 JS */
(() => {
  "use strict";

  let state = null;
  const filter = { recipe_id: null, zone_id: null };

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
    if (!res.ok) throw new Error((data && data.error) || `请求失败 (${res.status})`);
    return data;
  }

  const escapeHtml = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------ 加载

  function stateUrl(extra = {}) {
    const p = new URLSearchParams();
    const rid = extra.recipe_id ?? filter.recipe_id;
    const zid = extra.zone_id ?? filter.zone_id;
    if (rid) p.set("recipe_id", rid);
    if (zid) p.set("zone_id", zid);
    const qs = p.toString();
    return "/api/fertilizer/state" + (qs ? "?" + qs : "");
  }

  async function load(extra) {
    state = await api(stateUrl(extra));
    if (state.selected_id) filter.recipe_id = state.selected_id;
    renderAll();
  }

  const sel = () => state.selected;

  function renderAll() {
    renderSelectors();
    renderRecipeList();
    renderRecipeForm();
    renderOverview();
    renderConflict();
    renderTanks();
    renderComponents();
    renderItemTable();
    renderPairList();
  }

  // ------------------------------------------------------------ 选择器

  function renderSelectors() {
    const fzSel = $("#filter-zone"), fr = $("#filter-recipe"), rz = $("#recipe-zone");
    fzSel.innerHTML = `<option value="">全部分区</option>` +
      state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("");
    fzSel.value = state.filter_zone_id || "";

    fr.innerHTML = state.recipes.length
      ? state.recipes.map((r) =>
          `<option value="${r.id}"${r.id === state.selected_id ? " selected" : ""}>` +
          `${escapeHtml(r.name)}（${escapeHtml(zoneNameOf(r))}）</option>`).join("")
      : `<option value="">（无配方）</option>`;

    rz.innerHTML = state.zones.length
      ? state.zones.map((z) => `<option value="${z.id}">${escapeHtml(z.name)}</option>`).join("")
      : `<option value="">（请先建立分区）</option>`;

    $("#item-card").style.opacity = state.selected_id ? "1" : ".55";
    $("#pair-card").style.opacity = state.selected_id ? "1" : ".55";

    // 不相容关系的两个下拉
    const s = sel();
    const opts = s
      ? s.items.map((i) =>
          `<option value="${i.id}">${escapeHtml(i.name)}（${i.amount} mL）</option>`).join("")
      : "";
    $("#pair-a").innerHTML = opts || `<option value="">（先添加肥料）</option>`;
    $("#pair-b").innerHTML = opts || `<option value="">（先添加肥料）</option>`;
  }

  // zone_name 在卡片里没有；从 zones 补
  function zoneNameOf(r) {
    const z = state.zones.find((x) => x.id === r.zone_id);
    return z ? z.name : "（分区已删除）";
  }

  // ------------------------------------------------------------ 配方列表

  function renderRecipeList() {
    const ul = $("#recipe-list");
    ul.innerHTML = "";
    if (!state.recipes.length) {
      ul.innerHTML = `<li class="entity-row" style="color:var(--ink-soft)">` +
        (state.zones.length ? "没有配方，请在下方新建"
                            : "请先在轮灌编排台建立温室分区") + `</li>`;
      return;
    }
    state.recipes.forEach((r) => {
      const li = document.createElement("li");
      li.className = "entity-row recipe-item" + (r.id === state.selected_id ? " selected" : "");
      const usage = r.used_a == null ? "—" : `${r.used_a}/${r.used_b} mL`;
      li.innerHTML =
        `<span class="line1">
           <span class="grow name">${escapeHtml(r.name)}</span>
           <span class="zone-chip">${escapeHtml(zoneNameOf(r))}</span>
         </span>
         <span class="meta">
           <span><i class="status-dot ${r.status}"></i>${escapeHtml(r.status_label)}</span>
           <span>A/B ${r.cap_a}/${r.cap_b} mL</span>
         </span>
         <span class="meta">参与 ${r.active_count} 种 · 占用 ${usage} · 分量 ${r.component_count} 个</span>`;
      li.addEventListener("click", () => selectRecipe(r.id));
      ul.appendChild(li);
    });
  }

  async function selectRecipe(id) {
    filter.recipe_id = id;
    filter.zone_id = null;
    try { await load({ zone_id: undefined }); }
    catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 配方表单

  function renderRecipeForm() {
    const f = $("#recipe-form");
    const s = sel();
    $("#btn-recipe-delete").disabled = !s;
    if (!s) {
      f.elements.id.value = "";
      f.elements.name.value = "";
      f.elements.note.value = "";
      f.elements.cap_a.value = 2000;
      f.elements.cap_b.value = 2000;
      if (state.zones.length) f.elements.zone_id.value = state.zones[0].id;
      $("#recipe-submit").textContent = "新建配方";
      $("#recipe-cancel").classList.add("hidden");
      return;
    }
    f.elements.id.value = s.id;
    f.elements.name.value = s.name;
    f.elements.note.value = s.note || "";
    f.elements.zone_id.value = s.zone_id || "";
    f.elements.cap_a.value = s.cap_a;
    f.elements.cap_b.value = s.cap_b;
    $("#recipe-submit").textContent = "保存配方并重算";
    $("#recipe-cancel").classList.remove("hidden");
  }

  // ------------------------------------------------------------ 总览

  function renderOverview() {
    const s = sel();
    const title = $("#fert-title"), badge = $("#fert-badge");
    const meta = $("#fert-meta"), stats = $("#fert-stats"), box = $("#status-box");
    stats.innerHTML = ""; box.innerHTML = ""; badge.className = "case-badge";
    if (!s) {
      title.textContent = "未选择配方";
      badge.textContent = "";
      meta.textContent = "在左侧新建配方或从列表中选择。";
      return;
    }
    const r = s.result;
    title.textContent = `${s.name}（${escapeHtml(s.zone_name)}）`;
    badge.classList.add(r.status);
    badge.textContent = r.status_label;
    meta.innerHTML =
      `A 罐容量 <b>${s.cap_a} mL</b> · B 罐容量 <b>${s.cap_b} mL</b>` +
      `${s.note ? ` · ${escapeHtml(s.note)}` : ""}` +
      ` · 参与求解 ${r.active.length} 种（用量为 0 的 ${r.ignored.length} 种不参与）` +
      ` · 连通分量 ${r.components.length} 个`;

    const items = [
      ["母液总量", r.total_amount, "mL"],
      ["A 罐占用", r.decision ? r.decision.chosen.used_a : "—", "/ " + s.cap_a + " mL"],
      ["B 罐占用", r.decision ? r.decision.chosen.used_b : "—", "/ " + s.cap_b + " mL"],
      ["两罐体积差", r.decision ? r.decision.chosen.diff : "—", "mL"],
    ];
    items.forEach(([label, val, unit]) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="stat-label">${label}</span>` +
        `<span class="stat-val">${escapeHtml(String(val))} <span class="stat-unit">${unit}</span></span>`;
      stats.appendChild(li);
    });

    box.className = "forecast-box";
    if (r.status === "ok") {
      box.classList.add("ok");
      const c = r.decision.chosen;
      box.innerHTML = `✅ <strong>存在满足双罐容量的分罐方案</strong>` +
        `<div class="fc-detail">在 ${r.decision.combination_count} 种全局翻转组合中有 ` +
        `${r.decision.feasible_count} 种可行；已选两罐体积差最小的组合（差 ${c.diff} mL），` +
        `同分时按 A 罐肥料 ID 升序序列决胜。</div>`;
    } else if (r.status === "overflow") {
      box.classList.add("no");
      const c = r.decision.chosen;
      box.innerHTML = `🟠 <strong>双罐容量均无法满足，已给出最小总超额方案</strong>` +
        `<div class="fc-detail">${r.decision.combination_count} 种组合全部超容量；` +
        `最小总超额 <b>${c.excess_total} mL</b>（A 超 ${c.excess_a}、B 超 ${c.excess_b}），` +
        `该方案下 A=${c.used_a} mL、B=${c.used_b} mL、体积差 ${c.diff} mL。请调大罐容量或减少用量。</div>`;
    } else if (r.status === "conflict") {
      box.classList.add("no");
      box.style.background = "var(--red-bg)";
      box.style.borderColor = "#f0b5af";
      box.style.color = "var(--red)";
      box.innerHTML = `🔴 <strong>不相容图含奇环（长度 ${r.conflict_ring.length}），无法二着色</strong>` +
        `<div class="fc-detail">环内奇数个不相容关系无法用两种颜色分开，必须先解除环上至少一条关系。</div>`;
    } else if (r.status === "error") {
      box.classList.add("no");
      box.style.background = "var(--red-bg)";
      box.style.borderColor = "#f0b5af";
      box.style.color = "var(--red)";
      box.innerHTML = `⛔ <strong>${escapeHtml(r.error)}</strong>`;
    } else {
      box.classList.add("done");
      box.innerHTML = `📝 <strong>暂无用量大于 0 的肥料</strong>` +
        `<div class="fc-detail">在左侧添加肥料并填写正用量后开始求解；用量为 0 的肥料不进不相容图。</div>`;
    }
  }

  // ------------------------------------------------------------ 冲突链

  function renderConflict() {
    const s = sel();
    const card = $("#conflict-card");
    if (!s || s.result.status !== "conflict") {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    const ring = s.result.conflict_ring;
    const chain = $("#conflict-chain");
    chain.innerHTML = "";
    ring.forEach((node, i) => {
      if (i > 0) {
        const edge = document.createElement("span");
        edge.className = "ring-edge";
        edge.textContent = "✕";
        chain.appendChild(edge);
      }
      const n = document.createElement("span");
      n.className = "ring-node";
      n.textContent = `#${node.id} ${node.name}`;
      chain.appendChild(n);
    });
    const close = document.createElement("span");
    close.className = "ring-edge";
    close.textContent = "✕";
    chain.appendChild(close);
    const back = document.createElement("span");
    back.className = "ring-node";
    back.textContent = `#${ring[0].id} ${ring[0].name}（闭环）`;
    chain.appendChild(back);
    const note = document.createElement("span");
    note.className = "ring-note";
    note.textContent = `最短冲突环，共 ${ring.length} 个节点 / ${ring.length} 条不相容关系；删除环上任意一条关系即可消除冲突。`;
    chain.appendChild(note);
    $("#conflict-hint").textContent =
      `冲突位于连通分量 #${s.result.conflict_component}；冲突期间不分罐，下方清单与明细不指定罐位。`;
  }

  // ------------------------------------------------------------ 分罐清单

  function renderTanks() {
    const s = sel();
    const card = $("#tank-card");
    if (!s) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const r = s.result;
    const showTank = r.decision != null;
    $("#tank-subtitle").textContent = showTank
      ? (r.status === "overflow"
          ? "容量无解：展示全部组合中总超额最小的分罐结果（不得只显示无法分配）"
          : "按全局翻转组合寻优得到的分罐结果")
      : (r.status === "conflict" ? "存在奇环冲突，暂不分罐" : "暂无参与求解的肥料");

    const d = r.decision;
    fillTank("a", s, d);
    fillTank("b", s, d);

    const comboBox = $("#combo-box");
    comboBox.innerHTML = "";
    if (d && d.enumerated && d.combinations.length > 1) {
      const det = document.createElement("details");
      det.className = "combo-details";
      const chosenKey = JSON.stringify(d.chosen.flips);
      const rows = d.combinations.map((c, idx) => {
        const key = JSON.stringify(c.flips);
        return `<tr class="${key === chosenKey ? "chosen" : ""} ${c.feasible ? "" : "bad"}">
          <td>${idx + 1}</td>
          <td>${c.feasible ? "✓ 可行" : "✕ 超额"}</td>
          <td>${c.used_a}</td><td>${c.used_b}</td><td>${c.diff}</td>
          <td>${c.excess_total}</td>
          <td>[${c.a_ids.join(", ")}]</td>
        </tr>`;
      }).join("");
      det.innerHTML = `<summary>查看全部 ${d.combination_count} 种翻转组合（可行 / 超额 / 体积差 / 超额合计 / A 罐 ID 序列）</summary>
        <div class="table-wrap"><table class="combo-table">
          <thead><tr><th>#</th><th>容量</th><th>A(mL)</th><th>B(mL)</th><th>体积差</th>
          <th>总超额</th><th>A 罐肥料 ID</th></tr></thead>
          <tbody>${rows}</tbody></table></div>`;
      comboBox.appendChild(det);
    } else if (d && !d.enumerated) {
      comboBox.textContent = `共 ${d.combination_count} 种组合（>128 种，仅返回最优组合，不在页面展开）。`;
    }
  }

  function fillTank(letter, s, d) {
    const cap = letter === "a" ? s.cap_a : s.cap_b;
    const col = $(`.tank-${letter}`);
    $(`#tank-${letter}-cap`).textContent = `容量 ${cap} mL`;
    const ul = $(`#tank-${letter}-items`);
    const foot = $(`#tank-${letter}-foot`);
    const fill = $(`#tank-${letter}-fill`);
    col.classList.remove("over");
    ul.innerHTML = "";
    if (!d) {
      foot.textContent = "—";
      fill.style.width = "0%";
      return;
    }
    const c = d.chosen;
    const rows = letter === "a" ? c.tank_a : c.tank_b;
    const used = letter === "a" ? c.used_a : c.used_b;
    const excess = letter === "a" ? c.excess_a : c.excess_b;
    if (!rows.length) {
      ul.innerHTML = `<li class="tank-empty">（空罐）</li>`;
    } else {
      // 显示每个肥料所属分量
      const compById = {};
      s.result.active.forEach((f) => { compById[f.id] = f.component; });
      rows.forEach((f) => {
        const li = document.createElement("li");
        li.innerHTML = `<span>#${f.id} ${escapeHtml(f.name)} <span class="f-comp">分量 #${compById[f.id] ?? "—"}</span></span>` +
                       `<span><b>${f.amount}</b> mL</span>`;
        ul.appendChild(li);
      });
    }
    fill.style.width = Math.min(100, Math.round((used / cap) * 100)) + "%";
    if (used > cap) {
      col.classList.add("over");
      fill.style.width = "100%";
      foot.innerHTML = `占用 <b>${used}</b> / ${cap} mL · 剩余 ${cap - used} mL · ` +
        `<span class="over-amt">超额 ${excess} mL</span>`;
    } else {
      foot.innerHTML = `占用 <b>${used}</b> / ${cap} mL · 剩余 <b>${cap - used}</b> mL`;
    }
  }

  // ------------------------------------------------------------ 连通分量

  function renderComponents() {
    const wrap = $("#component-list");
    const s = sel();
    wrap.innerHTML = "";
    if (!s) { wrap.innerHTML = `<div class="chart-empty">请先选择配方</div>`; return; }
    const r = s.result;
    if (!r.components.length) {
      wrap.innerHTML = `<div class="chart-empty">暂无用量大于 0 的肥料，没有连通分量。</div>`;
      return;
    }
    r.components.forEach((comp) => {
      const card = document.createElement("div");
      card.className = "comp-card" + (comp.conflict ? " conflict" : "");
      const nameById = Object.fromEntries(r.active.map((f) => [f.id, f.name]));
      const amountById = Object.fromEntries(r.active.map((f) => [f.id, f.amount]));
      const chips = (ids) => ids.map((id) =>
        `<span class="comp-chip">#${id} ${escapeHtml(nameById[id] ?? "")} · ${amountById[id]} mL</span>`).join("") ||
        `<span class="pair-empty">（空侧）</span>`;
      card.innerHTML =
        `<div class="comp-head">
           <b>分量 #${comp.index}</b>
           <span class="comp-tag ${comp.conflict ? "conflict" : ""}">${comp.conflict ? "含奇环冲突" : "二着色成功"}</span>
           <span class="meta">${comp.member_ids.length} 个节点 · 两侧 ${comp.sum0} / ${comp.sum1} mL</span>
         </div>
         <div class="comp-sides">
           <div class="comp-side s0"><span class="side-label">着色侧 0（可翻向 A 或 B）</span>${chips(comp.side0)}</div>
           <div class="comp-side s1"><span class="side-label">着色侧 1（与侧 0 异色）</span>${chips(comp.side1)}</div>
         </div>
         <div class="comp-sum">两种翻转方向：侧0→A/侧1→B，或侧0→B/侧1→A；全局组合枚举后决定实际方向。</div>`;
      wrap.appendChild(card);
    });
  }

  // ------------------------------------------------------------ 肥料明细表

  function renderItemTable() {
    const tbody = $("#item-rows"), sub = $("#item-subtitle");
    tbody.innerHTML = "";
    const s = sel();
    if (!s) { sub.textContent = ""; return; }
    const r = s.result;
    sub.textContent = `共 ${s.items.length} 种 · 参与 ${r.active.length} 种 · 用量为 0 的 ${r.ignored.length} 种不参与`;

    const activeById = Object.fromEntries(r.active.map((f) => [f.id, f]));
    const tankById = {};
    if (r.decision) {
      r.decision.chosen.tank_a.forEach((f) => { tankById[f.id] = "A"; });
      r.decision.chosen.tank_b.forEach((f) => { tankById[f.id] = "B"; });
    }
    const ringIds = new Set((r.conflict_ring || []).map((f) => f.id));

    if (!s.items.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="7">暂无肥料，请在左上方添加</td></tr>`;
      return;
    }
    s.items.forEach((item) => {
      const a = activeById[item.id];
      const tank = tankById[item.id] || "—";
      const inRing = ringIds.has(item.id);
      const tr = document.createElement("tr");
      if (inRing) tr.classList.add("ring-row");
      tr.innerHTML =
        `<td>${item.id}</td>` +
        `<td>${escapeHtml(item.name)} ${inRing ? `<span class="ring-mark">● 冲突环上</span>` : ""}</td>` +
        `<td><input class="edit-amt" type="number" min="0" step="1" value="${item.amount}" data-item-id="${item.id}"></td>` +
        `<td>${a ? "#" + a.component : "—"}</td>` +
        `<td>${a ? a.color : "—"}</td>` +
        `<td class="tank-${tank}">${tank === "—" ? "不参与 / 待定" : tank + " 罐"}</td>` +
        `<td>
           <button class="del-btn" data-del-item="${item.id}" title="删除该肥料（自动清理其不相容关系）">🗑</button>
         </td>`;
      const inp = tr.querySelector(".edit-amt");
      inp.addEventListener("change", () => saveAmount(item.id, inp));
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
      tr.querySelector("[data-del-item]").addEventListener("click", () => removeItem(item));
      tbody.appendChild(tr);
    });
  }

  async function saveAmount(id, inp) {
    const raw = inp.value;
    try {
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 0) throw new Error("所需母液量必须是非负整数（0 表示暂不参与）");
      await api(`/api/fertilizer/items/${id}`, { method: "PUT", body: { amount: n } });
      await load();
      toast("用量已保存，分罐结果已立即重算");
    } catch (e) {
      toast(e.message, true);
      await load();  // 还原输入框
    }
  }

  async function removeItem(item) {
    if (!confirm(`删除肥料「${item.name}」？其全部不相容关系会一并删除，分罐立即重算。`)) return;
    try {
      await api(`/api/fertilizer/items/${item.id}`, { method: "DELETE" });
      await load();
      toast("肥料已删除，分罐结果已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 不相容关系

  function renderPairList() {
    const ul = $("#pair-rows"), sub = $("#pair-subtitle");
    ul.innerHTML = "";
    const s = sel();
    if (!s) { sub.textContent = ""; return; }
    const r = s.result;
    const nameById = Object.fromEntries(s.items.map((i) => [i.id, i.name]));
    sub.textContent = `共 ${s.pairs.length} 条（参与求解的 ${r.edges.length} 条）`;
    if (!s.pairs.length) {
      ul.innerHTML = `<li class="pair-empty">暂无不相容关系；在左侧登记两种必须分罐的肥料。</li>`;
      return;
    }
    // s.pairs 是裸 id 对；用 r.edges 标 in_ring（仅参与图的边），其余灰显
    const edgeByPair = {};
    r.edges.forEach((e) => { edgeByPair[`${e.a_id}-${e.b_id}`] = e; });
    s.pairs.forEach((p) => {
      const key = `${p.a_id}-${p.b_id}`;
      const edge = edgeByPair[key];
      const li = document.createElement("li");
      if (edge && edge.in_ring) li.classList.add("in-ring");
      const bothActive = edge != null;
      li.title = bothActive ? "" : "一端肥料用量为 0，暂不参与求解";
      li.innerHTML =
        `<span>#${p.a_id} ${escapeHtml(nameById[p.a_id] ?? "?")}</span>` +
        `<span style="color:var(--red)">✕</span>` +
        `<span>#${p.b_id} ${escapeHtml(nameById[p.b_id] ?? "?")}</span>` +
        (bothActive ? "" : `<span class="meta" style="color:var(--ink-soft)">（暂不参与）</span>`) +
        `<button class="pair-del" data-del-pair="${p.id}" title="删除该不相容关系">✕</button>`;
      li.querySelector("[data-del-pair]").addEventListener("click", () => removePair(p));
      ul.appendChild(li);
    });
  }

  async function removePair(p) {
    try {
      await api(`/api/fertilizer/pairs/${p.id}`, { method: "DELETE" });
      await load();
      toast("不相容关系已删除，分罐结果已重算");
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ 表单

  function bindForms() {
    $("#recipe-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        name: f.elements.name.value.trim(),
        zone_id: Number(f.elements.zone_id.value),
        cap_a: Number(f.elements.cap_a.value),
        cap_b: Number(f.elements.cap_b.value),
        note: f.elements.note.value.trim(),
      };
      const id = f.elements.id.value;
      try {
        if (id) {
          await api(`/api/fertilizer/recipes/${id}`, { method: "PUT", body });
          toast("配方已保存，分罐结果已重算");
        } else {
          const r = await api("/api/fertilizer/recipes", { method: "POST", body });
          filter.recipe_id = r.selected_id;
          toast("配方已创建");
        }
        await load();
      } catch (err) { toast(err.message, true); }
    });

    $("#recipe-cancel").addEventListener("click", async () => {
      filter.recipe_id = null;
      try {
        await load({ recipe_id: undefined });
        $("#recipe-form").reset();
        $("#recipe-submit").textContent = "新建配方";
        $("#recipe-cancel").classList.add("hidden");
      } catch (e) { toast(e.message, true); }
    });

    $("#btn-recipe-delete").addEventListener("click", async () => {
      const s = sel();
      if (!s) return;
      if (!confirm(`删除配方「${s.name}」？其全部肥料与不相容关系将一并删除。`)) return;
      try {
        await api(`/api/fertilizer/recipes/${s.id}`, { method: "DELETE" });
        filter.recipe_id = null;
        await load({ recipe_id: undefined });
        toast("配方已删除");
      } catch (e) { toast(e.message, true); }
    });

    $("#item-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const s = sel();
      if (!s) { toast("请先选择配方", true); return; }
      const f = e.target;
      const body = {
        name: f.elements.name.value.trim(),
        amount: Number(f.elements.amount.value),
      };
      if (!body.name) { toast("请填写肥料名称", true); return; }
      try {
        await api(`/api/fertilizer/recipes/${s.id}/items`, { method: "POST", body });
        f.elements.name.value = "";
        f.elements.amount.value = 0;
        await load();
        toast("肥料已添加，分罐结果已重算");
      } catch (err) { toast(err.message, true); }
    });

    $("#pair-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const s = sel();
      if (!s) { toast("请先选择配方", true); return; }
      const body = {
        a_id: Number(e.target.elements.a_id.value),
        b_id: Number(e.target.elements.b_id.value),
      };
      try {
        await api(`/api/fertilizer/recipes/${s.id}/pairs`, { method: "POST", body });
        await load();
        toast("不相容关系已登记，分罐结果已重算");
      } catch (err) { toast(err.message, true); }
    });
  }

  // ------------------------------------------------------------ 筛选 / 导出

  function bindFilters() {
    $("#filter-zone").addEventListener("change", async () => {
      filter.zone_id = Number($("#filter-zone").value) || null;
      filter.recipe_id = null;
      try { await load({ recipe_id: undefined }); }
      catch (e) { toast(e.message, true); }
    });
    $("#btn-filter-clear").addEventListener("click", async () => {
      filter.zone_id = null; filter.recipe_id = null;
      try { await load({ recipe_id: undefined, zone_id: undefined }); }
      catch (e) { toast(e.message, true); }
    });
    $("#filter-recipe").addEventListener("change", async () => {
      const id = Number($("#filter-recipe").value) || null;
      if (!id) return;
      filter.recipe_id = id; filter.zone_id = null;
      try { await load({ zone_id: undefined }); }
      catch (e) { toast(e.message, true); }
    });
    $("#btn-export").addEventListener("click", () => {
      const p = new URLSearchParams();
      if (state.selected_id && !state.filter_zone_id) p.set("recipe_id", state.selected_id);
      else if (state.filter_zone_id) p.set("zone_id", state.filter_zone_id);
      window.location.href = "/api/fertilizer/export?" + p.toString();
    });
  }

  // ------------------------------------------------------------ 启动

  bindForms();
  bindFilters();
  load().catch((e) => toast(e.message, true));
})();
