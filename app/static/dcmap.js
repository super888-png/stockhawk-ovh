/* Dashboard datacenter map. Land shapes: /static/worldmap.svg (Natural Earth, public domain, Miller projection).
   Data: /api/stock/map (per-datacenter stock aggregated from the shared poller). No external requests, no libraries. */
(function () {
  const root = document.getElementById("dcmap");
  if (!root) return;
  const NS = "http://www.w3.org/2000/svg";
  const $ = id => document.getElementById(id);
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  };
  // Label side per site so neighbours (gra/rbx, lon, bhs/tor/vin) don't collide. [dx, dy, anchor]
  const LBL = {gra: [0, -13, "middle"], rbx: [9, 2, "start"], lon: [-9, 3, "end"], eri: [-9, 14, "end"], par: [-9, 10, "end"],
    sbg: [9, 10, "start"], fra: [9, 4, "start"], lim: [9, -8, "start"], waw: [-9, -9, "end"],
    bhs: [8, -5, "start"], tor: [-8, -4, "end"], vin: [8, 9, "start"], hil: [8, 4, "start"],
    sgp: [8, 4, "start"], syd: [8, 4, "start"], ynm: [-8, 4, "end"]};
  const views = {};
  let sites = [], built = "", levels = {}, active = null, pinned = false;

  const miller = lat => {
    lat = Math.max(-85, Math.min(85, lat));
    return 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * Math.PI / 180)) * 180 / Math.PI;
  };
  function makeView(id, svg, box, d, opts) {
    const [lon0, lon1, lat0, lat1, w, h] = box.split(",").map(Number);
    const k = w / (lon1 - lon0), y0 = miller(lat1);
    const xy = (lat, lon) => [(lon - lon0) * k, (y0 - miller(lat)) * k];
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    const defs = el("defs", {}, svg);
    const pat = el("pattern", {id: id + "-dots", width: opts.dot, height: opts.dot, patternUnits: "userSpaceOnUse"}, defs);
    el("circle", {cx: opts.dot / 2, cy: opts.dot / 2, r: opts.dot * (id === "e" ? 0.27 : 0.3)}, pat);
    const grid = el("g", {class: "dcm-grid"}, svg);
    for (let lon = Math.ceil(lon0 / opts.step) * opts.step; lon <= lon1; lon += opts.step) {
      const [x] = xy(0, lon); el("line", {x1: x, x2: x, y1: 0, y2: h}, grid);
    }
    for (let lat = Math.ceil(lat0 / opts.step) * opts.step; lat <= lat1; lat += opts.step) {
      const [, y] = xy(lat, 0); el("line", {x1: 0, x2: w, y1: y, y2: y, class: lat === 0 ? "eq" : ""}, grid);
    }
    el("path", {d, class: "dcm-land", fill: `url(#${id}-dots)`}, svg);
    const deco = el("g", {class: "dcm-deco"}, svg);
    const layer = el("g", {class: "dcm-markers"}, svg);
    views[id] = {svg, xy, layer, deco, w, h, opts};
  }

  async function init() {
    const txt = await (await fetch("/static/worldmap.svg?v=1")).text();
    const doc = new DOMParser().parseFromString(txt, "image/svg+xml").documentElement;
    makeView("w", $("dcm-world"), doc.dataset.world, doc.querySelector("#world").getAttribute("d"), {dot: 6.5, step: 30, r: 6});
    makeView("e", $("dcm-eu"), doc.dataset.europe, doc.querySelector("#europe").getAttribute("d"), {dot: 8, step: 5, r: 8});
    // Europe zoom frame on the world map (matches the zoom panel's box)
    const [lo0, lo1, la0, la1] = doc.dataset.europe.split(",").map(Number);
    const [x0, y0] = views.w.xy(la1, lo0), [x1, y1] = views.w.xy(la0, lo1);
    el("rect", {x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 2, class: "dcm-frame"}, views.w.deco);
    const t = el("text", {x: x0, y: y0 - 4, class: "dcm-frame-t"}, views.w.deco); t.textContent = "EU";
    await load();
    setInterval(load, 15000);
  }

  function marker(view, s, mini) {
    const [x, y] = view.xy(s.lat, s.lon), r = mini ? 2.6 : view.opts.r;
    const g = el("g", {class: "dcm-m" + (mini ? " mini" : ""), transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`,
      "data-code": s.code}, view.layer);
    if (!mini) {
      g.setAttribute("tabindex", "0"); g.setAttribute("role", "button");
      el("circle", {class: "halo", r: r * 2.6}, g);
      el("circle", {class: "ring", r: r}, g);
      el("circle", {class: "ring r2", r: r}, g);
    }
    el("circle", {class: "core", r}, g);
    if (!mini) {
      const [dx, dy, an] = LBL[s.code] || [8, 4, "start"];
      const tx = el("text", {x: dx * (view.opts.r / 4), y: dy * (view.opts.r / 4) + (view === views.e ? 1 : 0), "text-anchor": an}, g);
      tx.textContent = s.label;
      el("circle", {class: "hit", r: Math.max(10, r * 3)}, g);
      g.addEventListener("mouseenter", () => { if (!pinned) show(s.code, g); });
      g.addEventListener("mouseleave", () => { if (!pinned) hide(); });
      g.addEventListener("focus", () => show(s.code, g));
      g.addEventListener("blur", () => { if (!pinned) hide(); });
      g.addEventListener("click", e => { e.stopPropagation(); pinned = !(pinned && active === s.code); pinned ? show(s.code, g) : hide(); });
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); g.dispatchEvent(new MouseEvent("click")); } });
    }
    return g;
  }

  function build() {
    for (const v of Object.values(views)) v.layer.textContent = "";
    for (const s of sites) {
      if (s.region === "eu") { marker(views.w, s, true); marker(views.e, s, false); }
      else marker(views.w, s, false);
    }
    built = sites.map(s => s.code).sort().join(",");
  }

  const lvl = s => s.monitored ? s.level : "unknown";
  function paint() {
    const by = Object.fromEntries(sites.map(s => [s.code, s]));
    root.querySelectorAll(".dcm-m").forEach(g => {
      const s = by[g.dataset.code]; if (!s) return;
      g.setAttribute("class", g.getAttribute("class").replace(/\s*lv-\w+|\s*unmon/g, "") + " lv-" + lvl(s) + (s.monitored ? "" : " unmon"));
      if (!g.classList.contains("mini")) g.setAttribute("aria-label", `${s.label} ${s.city} ${levels[lvl(s)] || ""}`);
    });
    $("dcm-list").innerHTML = sites.map(s => `<button type="button" class="dcm-chip lv-${lvl(s)}" data-code="${esc(s.code)}">
      <i></i><b>${esc(s.label)}</b><span>${esc(s.city.split(" ")[0])}</span><em>${s.monitored ? (s.inStock ? s.inStock + T(' 款有货') : esc(levels[s.level])) : T('未监控')}</em></button>`).join("");
    if (active) { const g = root.querySelector(`.dcm-m:not(.mini)[data-code="${active}"]`); if (g) show(active, g); }
  }

  function tipHTML(s) {
    const head = `<div class="dcm-tip-h"><span class="dcm-dot lv-${lvl(s)}"></span><b>${esc(s.label)}</b><span>${esc(s.city)} · ${esc(s.country)}</span></div>`;
    if (!s.monitored) return head + `<div class="dcm-tip-e">${T('当前范围内的型号没有在这个机房上架')}</div>`;
    const c = s.counts;
    const sum = `<div class="dcm-tip-s">${esc(levels[s.level])} ${T('· {0} 个配置：现货 {1} · 等交付 {2} · 无货', s.configs, c.high + c.low, c.wait)} ${c.none}${s.zones.length > 1 || (s.zones[0] && s.zones[0] !== s.code) ? ` ${T('· 可用区')} ${s.zones.map(esc).join(" ")}` : ""}</div>`;
    const rows = s.plans.map(p => `<tr><td><b>${esc(p.name)}</b>${p.cpu ? `<small>${esc(p.cpu)}</small>` : ""}</td><td><span class="dcm-av lv-${esc(p.level)}" title="${esc(p.best)}"><i class="dcm-dot"></i>${esc(avText(p.best))}</span></td>
      <td class="num">${p.inStock}/${p.total}</td></tr>`).join("");
    return head + sum + `<table><thead><tr><th>${T('型号')}</th><th>${T('最佳')}</th><th class="num">${T('有货配置')}</th></tr></thead><tbody>${rows}</tbody></table>`
      + (s.moreplans ? `<div class="dcm-tip-e">${T('还有 {0} 个型号，', s.moreplans)}<a href="/stock">${T('看完整列表')}</a></div>` : "");
  }
  function show(code, g) {
    const s = sites.find(x => x.code === code); if (!s) return;
    active = code; const tip = $("dcm-tip");
    root.querySelectorAll(".dcm-m.on").forEach(n => n.classList.remove("on")); g.classList.add("on");
    tip.innerHTML = tipHTML(s); tip.hidden = false;
    const box = root.querySelector(".dcmap").getBoundingClientRect(), m = g.querySelector(".core").getBoundingClientRect();
    const cx = m.left + m.width / 2 - box.left, cy = m.top + m.height / 2 - box.top;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = cx + 14, top = cy - th / 2;
    if (left + tw > box.width) left = cx - tw - 14;
    if (left < 0) left = Math.max(0, Math.min(box.width - tw, cx - tw / 2));
    top = Math.max(0, Math.min(top, box.height - th));
    if (left < cx && cx < left + tw && top < cy && cy < top + th) top = cy + 14;   // narrow screens: drop below
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function hide() {
    active = null; pinned = false; $("dcm-tip").hidden = true;
    root.querySelectorAll(".dcm-m.on").forEach(n => n.classList.remove("on"));
  }
  document.addEventListener("click", e => { if (pinned && !e.target.closest("#dcm-tip")) hide(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  $("dcm-list").addEventListener("click", e => {
    const b = e.target.closest(".dcm-chip"); if (!b) return; e.stopPropagation();
    const g = root.querySelector(`.dcm-m:not(.mini)[data-code="${b.dataset.code}"]`); if (!g) return;
    pinned = true; show(b.dataset.code, g);
    if (window.innerWidth < 720) g.closest(".dcm-panel").scrollIntoView({block: "nearest", behavior: "smooth"});
  });

  async function load() {
    try {
      const r = await fetch("/api/stock/map"); if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      levels = Object.fromEntries(d.levels.map(l => [l.key, l.label]));
      if (!$("dcm-legend").childElementCount)
        $("dcm-legend").innerHTML = d.levels.map(l => `<span title="${esc(l.values)}"><i class="dcm-dot lv-${esc(l.key)}"></i>${esc(l.label)}</span>`).join("");
      sites = d.sites.filter(s => s.lat != null);
      if (sites.map(s => s.code).sort().join(",") !== built) build();
      paint();
      const up = sites.filter(s => s.monitored && s.inStock).length;
      $("mscope").textContent = `${d.scope.label} ${d.scope.kind === "demo" ? "" : d.scope.plans + T(' 个')} ${T('· {0} 个机房有现货', up)}`;
      $("mtime").textContent = d.loading ? T('首次加载中…') : T('更新于 ') + fmtLocal(d.nowTs);
      $("mdot").className = "dot" + (d.loading ? " off" : "");
    } catch (e) {
      $("mtime").textContent = T('暂时无法获取'); $("mdot").className = "dot off";
    }
  }
  init().catch(() => { $("mtime").textContent = T('地图加载失败'); });
})();
