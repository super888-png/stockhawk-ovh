/* Shared UI helpers. No external dependencies. */
/* i18n: message ids are the Chinese strings; base.html injects the English catalog as JSON (not executed => CSP-safe). */
const LANG = document.documentElement.lang === "zh-CN" ? "zh" : "en";
let I18N = null;
function T(s, ...a) {
  if (I18N === null) { try { const el = document.getElementById("i18n-data"); I18N = el ? JSON.parse(el.textContent) : {}; } catch (e) { I18N = {}; } }
  const t = (LANG === "en" && Object.prototype.hasOwnProperty.call(I18N, s)) ? I18N[s] : s;
  return a.length ? t.replace(/\{(\d+)\}/g, (m, i) => (a[+i] ?? "")) : t;
}
const ICON = (n, cls = "") => `<svg class="ic ${cls}"><use href="/static/icons.svg#${n}"/></svg>`;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
function avClass(a) {
  if (!a || a === "unavailable" || a === "unknown") return "av-off";
  if (a.startsWith("1H")) return "av-hot";
  if (a === "24H") return "av-24";
  if (a === "72H") return "av-72";
  if (a === "comingSoon") return "av-soon";
  return "av-long";
}
function avText(a) {
  return ({"1H-high": T('现货·充足'), "1H-low": T('现货·少量'), "24H": T('24小时'), "72H": T('72小时'), "240H": T('10天'), "480H": T('20天'),
           "unavailable": T('无货'), "unknown": T('未知'), "comingSoon": T('即将上架')})[a] || a;
}
const avBadge = a => `<span class="av ${avClass(a)}" title="${esc(a)}">${esc(avText(a))}</span>`;

/* Searchable multi-select with chips (priority = chip order). */
class PlanPicker {
  constructor(root, opts) {
    this.root = root; this.opts = opts; this.all = []; this.sel = opts.initial || []; this.act = -1;
    root.classList.add("picker");
    root.innerHTML = `<div class="box"><input placeholder="${esc(opts.placeholder || T('搜索型号，如 RISE、ADVANCE、KS-LE…'))}"></div><div class="dd"></div>`;
    this.box = root.querySelector(".box"); this.input = root.querySelector("input"); this.dd = root.querySelector(".dd");
    // Open on focus, click (the input may still be focused after a pick) and typing.
    this.box.addEventListener("click", e => { if (!e.target.closest(".chip")) { this.input.focus(); this.open(); } });
    this.input.addEventListener("focus", () => this.open());
    this.input.addEventListener("input", () => { this.act = -1; this.open(); });
    this.input.addEventListener("keydown", e => this.key(e));
    // Outside-click check in the CAPTURE phase: an option re-renders the list on mousedown, so by the time a
    // bubbling handler ran, e.target would be detached and look "outside" (closed the picker after every add).
    document.addEventListener("mousedown", e => {
      if (this.isOpen() && e.target.isConnected && !root.contains(e.target)) this.close();
    }, true);
    this.input.addEventListener("blur", () => setTimeout(() => { if (!root.contains(document.activeElement)) this.close(); }, 150));
    window.addEventListener("resize", () => this.root.classList.contains("open") && this.place());
    window.addEventListener("scroll", () => this.root.classList.contains("open") && this.place(), {passive: true});
    this.renderChips();
  }
  setOptions(list) { this.all = list; this.sel = this.sel.map(s => list.find(x => x.planCode === s.planCode) ? {...list.find(x => x.planCode === s.planCode), ...s} : s); this.renderChips(); this.renderDD(); }
  isOpen() { return this.root.classList.contains("open"); }
  open() {
    // The dropdown is absolutely positioned inside its card. Cards don't create stacking contexts, and the
    // open picker's card is lifted above the following cards (.raised; CSS :has() does the same).
    this.root.classList.add("open"); this.renderDD(); this.place();
    const card = this.root.closest(".card"); if (card) card.classList.add("raised");
  }
  close() {
    this.root.classList.remove("open", "up"); this.act = -1;
    const card = this.root.closest(".card"); if (card) card.classList.remove("raised");
  }
  place() {  // open upwards when there is not enough room below; never run past the viewport
    const r = this.box.getBoundingClientRect(), below = window.innerHeight - r.bottom - 12;
    const bar = document.querySelector(".side"), top = r.top - 12 - (bar && getComputedStyle(bar).position === "sticky" && bar.getBoundingClientRect().height < 120 ? bar.getBoundingClientRect().height : 0);
    const up = below < 240 && top > below;
    this.root.classList.toggle("up", up);
    this.dd.style.maxHeight = Math.max(160, Math.min(360, up ? top : below)) + "px";
  }
  filtered() {
    const q = this.input.value.trim().toLowerCase(), tf = this.opts.typeFilter ? this.opts.typeFilter() : "all";
    return this.all.filter(p => (tf === "all" || p.type === tf) && (!q || (p.label + " " + p.planCode).toLowerCase().includes(q))).slice(0, 200);
  }
  renderDD() {
    const list = this.filtered(), selSet = new Set(this.sel.map(s => s.planCode));
    this.dd.innerHTML = list.length ? list.map((p, i) => `<div class="opt ${selSet.has(p.planCode) ? "sel" : ""} ${i === this.act ? "act" : ""}" data-i="${i}">
      ${ICON(selSet.has(p.planCode) ? "check" : "plus")}<b>${esc(p.name.split(" |")[0])}</b>
      <span class="grow">${esc((p.name.split(" | ")[1] || ""))}</span><span class="tag gray">${esc(p.typeLabel || p.type)}</span>
      <span class="p">${esc(p.planCode)}</span><span class="price">${p.monthly != null ? (this.opts.currency || "€") + p.monthly.toFixed(2) : ""}</span></div>`).join("")
      : `<div class="empty">${this.all.length ? T('没有匹配的型号') : T('目录加载中…')}</div>`;
    this.dd.querySelectorAll(".opt").forEach(el => el.addEventListener("mousedown", e => { e.preventDefault(); this.toggle(list[+el.dataset.i]); }));
    const a = this.dd.querySelector(".opt.act"); if (a) a.scrollIntoView({block: "nearest"});
  }
  toggle(p) {
    const i = this.sel.findIndex(s => s.planCode === p.planCode);
    if (i >= 0) this.sel.splice(i, 1);
    else if (this.sel.length < (this.opts.max || 20)) this.sel.push({planCode: p.planCode, type: p.type, label: p.name.split(" |")[0], name: p.name, monthly: p.monthly, memory: "", storage: "", bandwidth: ""});
    this.input.value = ""; this.renderChips(); this.renderDD(); this.changed();
    // keep picking: stay open with the caret in the input
    if (document.activeElement !== this.input) this.input.focus({preventScroll: true});
    this.open();
  }
  move(i, d) { const j = i + d; if (j < 0 || j >= this.sel.length) return; [this.sel[i], this.sel[j]] = [this.sel[j], this.sel[i]]; this.renderChips(); this.changed(); }
  key(e) {
    const list = this.filtered();
    if (!this.isOpen() && !["Escape", "Tab", "Shift", "Control", "Alt", "Meta"].includes(e.key)) this.open();
    if (e.key === "ArrowDown") { this.act = Math.min(list.length - 1, this.act + 1); this.renderDD(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { this.act = Math.max(0, this.act - 1); this.renderDD(); e.preventDefault(); }
    else if (e.key === "Enter") { e.preventDefault(); if (list[this.act >= 0 ? this.act : 0]) this.toggle(list[this.act >= 0 ? this.act : 0]); }
    else if (e.key === "Backspace" && !this.input.value && this.sel.length) { this.sel.pop(); this.renderChips(); this.changed(); }
    else if (e.key === "Escape") { this.close(); }
    else if (e.key === "Tab") this.close();
  }
  renderChips() {
    this.box.querySelectorAll(".chip").forEach(c => c.remove());
    this.sel.forEach((s, i) => {
      const c = document.createElement("span"); c.className = "chip";
      c.innerHTML = `${this.opts.ordered === false ? "" : `<span class="n">${i + 1}</span>`}${esc(s.label || s.planCode)} <small>${esc(s.planCode)}</small>
        ${this.opts.ordered === false ? "" : `<button type="button" title="${T('提高优先级')}" aria-label="${T('提高优先级')}" data-a="l">${ICON("arrow-left")}</button><button type="button" title="${T('降低优先级')}" aria-label="${T('降低优先级')}" data-a="r">${ICON("arrow-right")}</button>`}
        <button type="button" title="${T('移除')}" aria-label="${T('移除')}" data-a="x">${ICON("close")}</button>`;
      c.querySelectorAll("button").forEach(b => b.addEventListener("click", e => { e.stopPropagation();
        if (b.dataset.a === "x") { this.sel.splice(i, 1); this.renderChips(); this.renderDD(); this.changed(); } else this.move(i, b.dataset.a === "l" ? -1 : 1); }));
      this.box.insertBefore(c, this.input);
    });
  }
  changed() { this.opts.onChange && this.opts.onChange(this.sel); }
}

/* Theme switch buttons (state lives in the inline <head> script: window.__theme). */
function syncThemeButtons() {
  const p = window.__theme ? window.__theme.pref() : "system";
  document.querySelectorAll("[data-theme-set]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.themeSet === p)));
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-theme-set]");
  if (b && window.__theme) { window.__theme.set(b.dataset.themeSet); syncThemeButtons(); }
});
document.addEventListener("DOMContentLoaded", syncThemeButtons);
window.addEventListener("storage", syncThemeButtons);

/* ---- times in the browser's own time zone ---- */
const pad2 = n => String(n).padStart(2, "0");
function fmtLocal(iso, withYear = false) {
  if (!iso) return "";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  if (isNaN(d)) return esc(iso);
  const md = `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  return `${withYear ? d.getFullYear() + "-" : ""}${md} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
function localizeTimes(root = document) {
  const now = new Date();
  root.querySelectorAll("time[data-lt]").forEach(t => {
    const d = new Date(t.getAttribute("datetime"));
    if (isNaN(d)) return;
    const s = fmtLocal(t.getAttribute("datetime"), d.getFullYear() !== now.getFullYear());
    t.textContent = s.slice(0, s.length - 3);   // minutes are enough in tables
    t.title = d.toLocaleString();
  });
}
document.addEventListener("DOMContentLoaded", () => {
  localizeTimes();
  const tz = document.getElementById("tzname");
  try { if (tz) tz.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone || tz.textContent; } catch (e) {}
});

/* ---- confirmations without inline handlers (strict CSP) ---- */
document.addEventListener("submit", e => {
  const msg = e.submitter?.dataset.confirm || e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
}, true);

/* ---- 进行中的任务 strip (task pages + dashboard) ---- */
(function () {
  const MODE_CLS = {notify: "green", dry: "cyan", order: "amber", pay: "red"};
  let busy = false, data = null, recv = 0;
  function ago(s) { return s == null ? "-" : s < 60 ? `${s} ${T('秒前')}` : `${Math.floor(s / 60)} ${T('分钟前')}`; }
  function render() {
    const root = document.getElementById("runstrip"); if (!root || !data) return;
    const cur = root.dataset.current, list = document.getElementById("rs-list"), n = data.tasks.length;
    document.getElementById("rs-count").textContent = n ? `${n} ${T('个运行中 · 实时更新')}` : T('当前没有运行中的任务');
    document.getElementById("rs-dot").className = n ? "dot" : "dot off";
    root.classList.toggle("empty", !n);
    const drift = (Date.now() - recv) / 1000;
    list.innerHTML = data.tasks.map(t => {
      const age = t.lastPoll ? Math.max(0, Math.round(data.now - t.lastPoll + drift)) : null;
      const stale = age != null && age > Math.max(30, t.interval * 4);
      return `<div class="rs-item${String(t.id) === cur ? " cur" : ""}${t.inStock ? " hit" : ""}">
        <a class="rs-main" href="/tasks/${t.id}"><b>#${t.id} ${esc(t.name || (t.plans[0] && t.plans[0].name) || "")}</b>
          <span class="rs-plans">${t.plans.map(p => `<span title="${esc(p.code)}${p.cpu ? " · " + esc(p.cpu) : ""}">${esc(p.name)}</span>`).join(" · ")}</span></a>
        <span class="tag ${MODE_CLS[t.mode] || "gray"}">${esc(t.modeCn)}</span>
        <span class="rs-k" title="${T('本次启动以来的轮询次数')}">${T('轮询')} <b>${t.polls}</b></span>
        <span class="rs-k${stale ? " warn" : ""}" title="${T('最近一次轮询')}">${T('心跳')} <b>${ago(age)}</b></span>
        <span class="rs-k">${t.inStock ? `<span class="tag green">${T('有货')}</span>` : T('无货')}${t.mode !== "dry" && t.mode !== "notify" ? ` ${T('· 已下单 {0}/', t.ordersDone)}${t.maxOrders}` : ""}</span>
        <button class="sm" data-stop="${t.id}" title="${T('停止任务')}">${ICON("pause-circle")} ${T('停止')}</button>
      </div>`;
    }).join("");
  }
  async function load() {
    if (busy || document.hidden) return; busy = true;
    try { const r = await fetch("/api/tasks/running"); if (r.ok) { data = await r.json(); recv = Date.now(); render(); } }
    catch (e) {} finally { busy = false; }
  }
  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-stop]"); if (!b) return;
    b.disabled = true;
    const fd = new FormData(); fd.append("csrf", window.CSRF || "");
    try { await fetch(`/tasks/${b.dataset.stop}/stop`, {method: "POST", body: fd, headers: {"X-Requested-With": "fetch"}}); } catch (_) {}
    if (document.getElementById("runstrip").dataset.current === b.dataset.stop) location.reload(); else load();
  });
  document.addEventListener("DOMContentLoaded", () => {
    if (!document.getElementById("runstrip")) return;
    load(); setInterval(load, 2000); setInterval(render, 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  });
})();

/* ---- CNY formatting shared by every price display: "≈ ¥1,029.38" ---- */
const yuan = v => "≈ ¥" + Number(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
