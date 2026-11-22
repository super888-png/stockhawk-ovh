/* Live-stock browser shared by 实时库存 (view) and 新建任务 (select). Markup: templates/_stockui.html.
   StockBrowser  = filters + model picker + live table (one shared poll via /api/stock, never per-row calls)
   TaskSelection = ordered task items {planCode,type,memory,storage,bandwidth,dcs[]} (order = buy priority)
   SelectionTray = the "Selected" list: reorder (drag / up-down), per-item config + datacenters, remove      */
const $id = id => document.getElementById(id);

class TaskSelection {
  constructor(items, onChange, max = 20) {
    this.items = (items || []).map(x => ({planCode: x.planCode, type: x.type || "eco", label: x.label || "", name: x.name || "",
      memory: x.memory || "", storage: x.storage || "", bandwidth: x.bandwidth || "", dcs: [...(x.dcs || [])],
      memL: x.memL || "", stoL: x.stoL || ""}));
    this.onChange = onChange || (() => {}); this.max = max;
  }
  // exact = this row's plan + config is pinned and its datacenter is in the item's list
  exact(r) { return this.items.findIndex(s => s.planCode === r.plan && s.memory === r.memory && s.storage === r.storage && s.dcs.includes(r.dc)); }
  // covered = an item with "any" memory/storage/datacenter would also buy this row
  covered(r) { return this.items.findIndex(s => s.planCode === r.plan && (!s.memory || s.memory === r.memory) && (!s.storage || s.storage === r.storage) && (!s.dcs.length || s.dcs.includes(r.dc))); }
  toggleRow(r) {
    const i = this.exact(r);
    if (i >= 0) {
      const s = this.items[i]; s.dcs = s.dcs.filter(d => d !== r.dc);
      if (!s.dcs.length) this.items.splice(i, 1);   // last datacenter removed => drop the item (never silently widen to "any")
    } else {
      const j = this.items.findIndex(s => s.planCode === r.plan && s.memory === r.memory && s.storage === r.storage && s.dcs.length);
      if (j >= 0) this.items[j].dcs.push(r.dc);     // same machine + config: one item, several datacenters
      else if (this.items.length < this.max) this.items.push({planCode: r.plan, type: r.type, label: r.name, name: r.name,
        memory: r.memory, storage: r.storage, bandwidth: "", dcs: [r.dc], memL: r.ram, stoL: r.disk});
      else return false;
    }
    this.onChange(); return true;
  }
  addWhole(p) {   // a whole model: any config (subject to "allow non-default"), any datacenter
    if (this.items.length >= this.max || this.items.some(s => s.planCode === p.planCode && !s.memory && !s.storage && !s.dcs.length)) return;
    this.items.push({planCode: p.planCode, type: p.type, label: p.label || p.name, name: p.name || p.label, memory: "", storage: "", bandwidth: "", dcs: [], memL: "", stoL: ""});
    this.onChange();
  }
  move(i, d) { const j = i + d; if (j < 0 || j >= this.items.length) return; [this.items[i], this.items[j]] = [this.items[j], this.items[i]]; this.onChange(); }
  moveTo(from, to) { if (from === to || from < 0 || to < 0) return; const [x] = this.items.splice(from, 1); this.items.splice(to, 0, x); this.onChange(); }
  remove(i) { this.items.splice(i, 1); this.onChange(); }
  payload() { return this.items.map(s => ({planCode: s.planCode, type: s.type, label: s.label || s.name || "", memory: s.memory, storage: s.storage, bandwidth: s.bandwidth, dcs: s.dcs})); }
}

class StockBrowser {
  /* o = {select, storageKey, subs, mine, selection, initialPlans, keepContext, onContext, onCatalog} */
  constructor(o) {
    this.o = o; this.last = null; this.timer = null; this.currency = "€"; this.plans = [];
    let saved = {}; try { saved = JSON.parse(localStorage.getItem(o.storageKey) || "{}"); } catch (e) {}
    this.saved = saved;
    if (!o.keepContext) { if (saved.endpoint) $id("endpoint").value = saved.endpoint; if (saved.subsidiary) $id("subsidiary").value = saved.subsidiary; }
    if (saved.ctype) $id("ctype").value = saved.ctype;
    ["onlyStock", "onlyDefault"].forEach(k => { if (k in saved) $id(k).checked = saved[k]; });
    const init = [...(o.initialPlans || [])];
    if (!init.length) init.push(...(saved.plans || []));
    const uniq = []; init.forEach(p => { if (p && p.planCode && !uniq.some(u => u.planCode === p.planCode)) uniq.push({planCode: p.planCode, type: p.type || "eco", label: p.label || p.planCode}); });
    this.picker = new PlanPicker($id("picker"), {initial: uniq, ordered: false, max: 30, typeFilter: () => $id("ctype").value,
      onChange: () => { this.save(); this.load(); }});
    this.bind(); this.fillSubs(false);
  }
  ep() { return $id("endpoint").value; }
  sub() { return $id("subsidiary").value; }
  save() {
    localStorage.setItem(this.o.storageKey, JSON.stringify({endpoint: this.ep(), subsidiary: this.sub(), ctype: $id("ctype").value,
      onlyStock: $id("onlyStock").checked, onlyDefault: $id("onlyDefault").checked,
      plans: this.picker.sel.map(s => ({planCode: s.planCode, type: s.type, label: s.label}))}));
  }
  fillSubs(fix) {
    const list = this.o.subs[this.ep()] || [];
    $id("subs").innerHTML = list.map(s => `<option value="${s}">`).join("");
    if (fix && list.length && !list.includes(this.sub().toUpperCase())) $id("subsidiary").value = list[0];
  }
  ensurePlan(pc, type, label) {   // deep links / saved items: make sure their model is listed in the table
    if (!this.picker.sel.some(s => s.planCode === pc)) { this.picker.sel.push({planCode: pc, type: type || "eco", label: label || pc}); this.picker.renderChips(); }
  }
  async loadCatalog() {
    const r = await fetch("/api/catalog?" + new URLSearchParams({endpoint: this.ep(), subsidiary: this.sub(), type: "all"}));
    const d = await r.json();
    if (!r.ok) { this.o.onCatalog && this.o.onCatalog(null, d); return; }
    this.currency = d.currency === "EUR" ? "€" : d.currency + " "; this.picker.opts.currency = this.currency;
    this.plans = d.plans; this.picker.setOptions(d.plans); this.renderWhole();
    this.o.onCatalog && this.o.onCatalog(d);
  }
  quick(prefix) {
    for (const p of this.picker.all.filter(p => p.name.toUpperCase().startsWith(prefix) && !/-(mum|sgp|syd)$/.test(p.planCode)))
      if (!this.picker.sel.some(s => s.planCode === p.planCode)) this.picker.toggle(p);
    this.picker.close();
  }
  async load() {
    clearTimeout(this.timer);
    const plans = this.picker.sel.map(s => s.planCode + ":" + s.type).join(",");
    this.renderWhole();
    if (!plans) {
      this.last = null;
      $id("rows").innerHTML = `<tr><td colspan="9" class="empty">${ICON("radar-chart")}${T('先在上方选择要监控的型号')}</td></tr>`;
      $id("dot").className = "dot off"; $id("upd").textContent = T('未开始'); $id("feedinfo").innerHTML = ""; $id("count").textContent = ""; return;
    }
    try {
      const r = await fetch("/api/stock?" + new URLSearchParams({endpoint: this.ep(), subsidiary: this.sub(), plans}));
      this.last = await r.json(); this.render();
      $id("dot").className = "dot"; $id("upd").textContent = T('实时 · ') + (this.last.nowTs ? fmtLocal(this.last.nowTs) : this.last.now);
    } catch (e) { $id("dot").className = "dot off"; $id("upd").textContent = T('连接中断，重试中…'); }
    this.timer = setTimeout(() => this.load(), (this.last && this.last.feeds && this.last.feeds.some(f => f.loading)) ? 1500 : 5000);
  }
  renderWhole() {
    const w = $id("wholebar"); if (!w || !this.o.selection) return;
    const sel = this.picker.sel;
    w.hidden = !sel.length;
    w.innerHTML = sel.length ? `<span class="muted small">${T('整个型号（任意配置 · 任意机房）：')}</span>` + sel.map(s => {
      const p = this.plans.find(x => x.planCode === s.planCode) || s, nm = (p.name || s.label || s.planCode).split(" |")[0];
      return `<button type="button" class="sm ghost" data-whole="${esc(s.planCode)}" title="${T('不限内存/硬盘/机房，整体作为一个优先级')}">${ICON("plus")} ${esc(nm)}</button>`; }).join("") : "";
    w.querySelectorAll("[data-whole]").forEach(b => b.onclick = () => {
      const s = sel.find(x => x.planCode === b.dataset.whole), p = this.plans.find(x => x.planCode === b.dataset.whole) || {};
      this.o.selection.addWhole({planCode: s.planCode, type: s.type, label: (p.name || s.label || s.planCode).split(" |")[0], name: (p.name || s.label || "").split(" |")[0]});
    });
  }
  rows() {
    if (!this.last || !this.last.rows) return [];
    const f = $id("filter").value.trim().toLowerCase(), os = $id("onlyStock").checked, od = $id("onlyDefault").checked, sel = this.o.selection;
    return this.last.rows.filter(x => (sel && sel.exact(x) >= 0) || ((!os || x.inStock) && (!od || x.default)
      && (!f || [x.dc, x.dcLabel, x.memory, x.storage, x.ram, x.disk, x.name, x.plan].join(" ").toLowerCase().includes(f))));
  }
  render() {
    const last = this.last; if (!last || !last.rows) return;
    const fx = last.fx, sel = this.o.selection, fxel = $id(this.o.fxId || "fxline");
    const cny = v => fx && fx.rate && fx.base !== "CNY" ? `<span class="cny">${yuan(v * fx.rate)}</span>` : "";
    if (fxel) fxel.innerHTML = fx && fx.rate ? `${ICON("swap")}${T('人民币按 1 {0} = {1} CNY 换算 ·', esc(fx.base), fx.rate.toFixed(4))} ${esc(fx.source)}${fx.fetchedIso ? T(' · 获取于 ') + fmtLocal(fx.fetchedIso) : ""}${fx.live ? "" : T('（汇率接口暂不可用，为估算值）')}` : "";
    const upd = Object.fromEntries(last.feeds.map(x => [x.plan, x]));
    const rows = this.rows();
    $id("feedinfo").innerHTML = last.feeds.map(x => `<span class="tag ${x.error ? "red" : x.loading ? "amber" : "gray"}" title="${esc(x.error || "")}">${esc(x.name)} · ${x.loading ? T('加载中') : x.error ? T('出错') : T('每 ') + x.interval + "s"}</span>`).join("");
    const total = last.rows.length, inst = last.rows.filter(x => x.inStock).length;
    $id("count").textContent = T('共 {0} 个 配置×机房 组合，其中 {1} 个有货；当前显示 {2} 条', total, inst, rows.length);
    this.shown = rows.slice(0, 600);
    const cols = sel ? 9 : 9;
    $id("rows").innerHTML = this.shown.length ? this.shown.map((x, i) => {
      const ex = sel ? sel.exact(x) : -1, cv = sel && ex < 0 ? sel.covered(x) : -1;
      const pick = sel ? `<td class="selcol"><span class="rowpick${ex >= 0 ? " on" : ""}${cv >= 0 ? " cov" : ""}" title="${ex >= 0 ? T('已选 · 优先级 {0}（再点一次取消）', ex + 1) : cv >= 0 ? T('已被第 {0} 项（任意配置/机房）覆盖；点选可单独指定', cv + 1) : T('加入任务')}"><input type="checkbox" class="vh" data-r="${i}" ${ex >= 0 ? "checked" : ""} aria-label="${T('加入任务')}"><span class="box">${ex >= 0 ? ex + 1 : cv >= 0 ? "≈" + (cv + 1) : ICON("plus")}</span></span></td>` : "";
      return `<tr class="${ex >= 0 ? "picked" : ""}" ${sel ? `data-r="${i}"` : ""}>${pick}
    <td class="name"><b>${esc(x.name)}</b><small>${esc(x.cpu)}</small></td>
    <td><code title="${x.type === "eco" ? T('Eco 目录') : T('Bare Metal 目录')}">${esc(x.plan)}</code></td>
    <td>${esc(x.dcLabel)}</td>
    <td title="${esc(x.memory)}">${esc(x.ram)}</td>
    <td title="${esc(x.storage)}">${x.default ? `<span class="tag green">${T('基础')}</span> ` : ""}${esc(x.disk)}</td>
    <td>${avBadge(x.availability)}<small class="upd" title="${T('该型号库存最近一次刷新时间')}">${(upd[x.plan] || {}).updatedTs ? fmtLocal(upd[x.plan].updatedTs).slice(6) : esc((upd[x.plan] || {}).updated || "")}</small>${x.orderable === false ? ` <span class="tag amber" title="${esc(x.why)}${T('。OVH 的库存接口列出了这个硬件，但目录和购物车里没有对应配件，无法单独下单')}">${T('不可下单')}</span>` : ""}</td>
    <td class="num price">${x.price != null ? esc(x.currency) + x.price.toFixed(2) + cny(x.price) : "–"}</td><td class="num price-first">${x.first != null ? esc(x.currency) + x.first.toFixed(2) + cny(x.first) : "–"}</td>
    ${sel ? "" : `<td>${x.orderable === false ? "" : `<a class="btn sm" href="/tasks/new?${new URLSearchParams({plan: x.plan, type: x.type, dc: x.dc, memory: x.memory, storage: x.storage, endpoint: this.ep(), subsidiary: this.sub()})}" title="${T('用这个配置新建任务')}">${T('新建任务')}</a>`}</td>`}</tr>`; }).join("")
      : `<tr><td colspan="${cols}" class="empty">${ICON("search")}${last.feeds.some(x => x.loading) ? T('首次加载中…') : T('没有符合条件的库存')}</td></tr>`;
  }
  bind() {
    $id("filter").addEventListener("input", () => this.render());
    ["onlyStock", "onlyDefault"].forEach(k => $id(k).addEventListener("change", () => { this.save(); this.render(); }));
    $id("ctype").addEventListener("change", () => { this.save(); this.picker.renderDD(); });
    ["endpoint", "subsidiary"].forEach(k => $id(k).addEventListener("change", () => {
      if (k === "endpoint") this.fillSubs(!!this.o.select); this.save(); this.loadCatalog(); this.load(); this.o.onContext && this.o.onContext(k); }));
    $id("quickRise").onclick = () => this.quick("RISE-");
    $id("quickAdv").onclick = () => this.quick("ADVANCE-");
    $id("clearSel").onclick = () => { this.picker.sel = []; this.picker.renderChips(); this.picker.renderDD(); this.save(); this.load(); };
    if ($id("importMine")) $id("importMine").onclick = () => {
      for (const m of this.o.mine || []) if (m.endpoint === this.ep() && !this.picker.sel.some(s => s.planCode === m.planCode)) this.picker.sel.push({planCode: m.planCode, type: m.type, label: m.label});
      this.picker.renderChips(); this.save(); this.load(); };
    if (this.o.selection) {   // whole row is the hit target; the checkbox keeps it keyboard-accessible
      $id("rows").addEventListener("click", e => {
        if (e.target.closest("a,button,select")) return;
        const tr = e.target.closest("tr[data-r]"); if (!tr) return;
        if (e.target.matches("input[data-r]")) e.preventDefault();
        const r = this.shown[+tr.dataset.r]; if (r && !this.o.selection.toggleRow(r)) alert(T('最多选择 {0} 项', this.o.selection.max));
      });
      $id("rows").addEventListener("keydown", e => { if ((e.key === " " || e.key === "Enter") && e.target.matches("input[data-r]")) { e.preventDefault(); e.target.closest("tr").click(); } });
    }
  }
}

class SelectionTray {
  /* root = <ol>; detail(item) -> /api/plan json (cached by caller); onEdit() after any config change */
  constructor(root, selection, detail, dcl, onEdit) {
    this.root = root; this.s = selection; this.detail = detail; this.dcl = dcl || {}; this.onEdit = onEdit || (() => {}); this.drag = -1;
    root.addEventListener("dragstart", e => { const li = e.target.closest("li[data-i]"); if (!li) return; this.drag = +li.dataset.i; li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", li.dataset.i); } catch (_) {} });
    root.addEventListener("dragend", e => { this.drag = -1; root.querySelectorAll(".dragging,.dropto").forEach(x => x.classList.remove("dragging", "dropto")); });
    root.addEventListener("dragover", e => { const li = e.target.closest("li[data-i]"); if (!li || this.drag < 0) return; e.preventDefault(); root.querySelectorAll(".dropto").forEach(x => x.classList.remove("dropto")); li.classList.add("dropto"); });
    root.addEventListener("drop", e => { const li = e.target.closest("li[data-i]"); if (!li || this.drag < 0) return; e.preventDefault(); const from = this.drag; this.drag = -1; this.s.moveTo(from, +li.dataset.i); });
  }
  opts(items, key, cur, first, curLabel) {
    const has = items.some(i => i[key] === cur);
    return `<option value="">${first}</option>` + (cur && !has ? `<option value="${esc(cur)}" selected>${esc(curLabel || cur)}</option>` : "")
      + items.map(i => `<option value="${esc(i[key])}" ${i[key] === cur ? "selected" : ""}>${esc(i.label)}${i.default ? T('（默认）') : ""}</option>`).join("");
  }
  async render() {
    const items = this.s.items, root = this.root;
    $id("selcount").textContent = items.length ? T('{0} 项', items.length) : "";
    if (!items.length) { root.innerHTML = `<li class="empty">${ICON("unordered-list")}${T('还没有选择。在上方表格点选 配置 × 机房，或用「整个型号」按钮')}</li>`; return; }
    const seq = (this.seq = (this.seq || 0) + 1);
    const ds = await Promise.all(items.map(it => this.detail(it).catch(() => null)));
    if (seq !== this.seq) return;
    root.innerHTML = items.map((it, i) => {
      const d = ds[i], f = (d && d.families) || {}, name = (d && d.name ? d.name.split(" |")[0] : (it.name || it.label || it.planCode));
      const cpu = d && d.name ? (d.name.split(" | ")[1] || "") : "";
      const allowed = d ? d.dcs : [], codes = [...allowed.map(x => x.code), ...it.dcs.filter(c => !allowed.some(a => a.code === c))];
      const dcs = codes.map(c => { const k = it.dcs.indexOf(c), bad = d && !allowed.some(a => a.code === c);
        return `<span class="dc sm ${k >= 0 ? "on" : ""}${bad ? " bad" : ""}" data-c="${esc(c)}" role="button" tabindex="0" aria-pressed="${k >= 0}" ${bad ? `title="${T('该型号不在这个机房销售')}"` : ""}><span class="o">${k + 1}</span>${esc(this.dcl[c] || (allowed.find(a => a.code === c) || {}).label || c)}</span>`; }).join("");
      return `<li class="selitem" data-i="${i}" draggable="true">
        <div class="si-head"><span class="grip" title="${T('拖动调整优先级')}" aria-hidden="true">${ICON("menu")}</span><span class="prio" title="${T('下单优先级')}">${i + 1}</span>
          <div class="si-name"><b>${esc(name)}</b> <code class="pc">${esc(it.planCode)}</code>${cpu ? `<small>${esc(cpu)}${d && d.monthly != null ? T(' · {0}{1}/月', d.currency, d.monthly.toFixed(2)) : ""}</small>` : ""}${d ? "" : `<small class="err">${T('目录中找不到该型号')}</small>`}</div>
          <div class="si-act"><button type="button" class="sm ghost" data-a="up" title="${T('提高优先级')}" aria-label="${T('提高优先级')}" ${i === 0 ? "disabled" : ""}>${ICON("arrow-up")}</button><button type="button" class="sm ghost" data-a="down" title="${T('降低优先级')}" aria-label="${T('降低优先级')}" ${i === items.length - 1 ? "disabled" : ""}>${ICON("arrow-down")}</button><button type="button" class="sm ghost" data-a="x" title="${T('移除')}" aria-label="${T('移除')}">${ICON("close")}</button></div></div>
        <div class="si-cfg">
          <label>${T('内存')}<select data-k="memory">${this.opts((f.memory || {items: []}).items, "fragment", it.memory, T('任意'), it.memL)}</select></label>
          <label>${T('硬盘')}<select data-k="storage">${this.opts((f.storage || {items: []}).items, "fragment", it.storage, T('任意'), it.stoL)}</select></label>
          <label>${T('带宽')}<select data-k="bandwidth">${this.opts((f.bandwidth || {items: []}).items, "code", it.bandwidth, T('目录默认'))}</select></label>
        </div>
        <div class="si-dcs"><span class="k">${T('机房')}</span><div class="dcs">${dcs || `<span class="muted small">${T('目录加载中…')}</span>`}</div><span class="muted small">${it.dcs.length ? T('优先级：') + it.dcs.map(c => this.dcl[c] || c).join(" → ") : T('未选择 = 任意机房')}</span></div>
      </li>`; }).join("");
    root.querySelectorAll("li[data-i]").forEach(li => {
      const i = +li.dataset.i, it = items[i];
      li.querySelectorAll("button[data-a]").forEach(b => b.onclick = () => b.dataset.a === "x" ? this.s.remove(i) : this.s.move(i, b.dataset.a === "up" ? -1 : 1));
      li.querySelectorAll("select[data-k]").forEach(sel => sel.onchange = () => { it[sel.dataset.k] = sel.value;
        if (sel.dataset.k !== "bandwidth") it[sel.dataset.k === "memory" ? "memL" : "stoL"] = sel.value ? sel.selectedOptions[0].textContent : ""; this.s.onChange(); });
      const flip = el => { const c = el.dataset.c; it.dcs = it.dcs.includes(c) ? it.dcs.filter(x => x !== c) : [...it.dcs, c]; this.s.onChange(); };
      li.querySelectorAll(".dc[data-c]").forEach(el => { el.onclick = () => flip(el); el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(el); } }; });
      // only the grip starts a drag, so selects / chips stay usable
      li.draggable = false; const g = li.querySelector(".grip");
      g.addEventListener("mousedown", () => li.draggable = true); g.addEventListener("touchstart", () => li.draggable = true, {passive: true});
      li.addEventListener("dragend", () => li.draggable = false);
    });
    this.onEdit();
  }
}
