/* 控制台「我的服务器」+ 订单记录「账号订单」: read-only OVH account data (cached server-side).
   Each page only has the slots it shows (servers on the dashboard, orders on /orders). */
(() => {
  const {appOrders, guide} = window.ACCT;
  const WANT = window.ACCT.want || ["servers", "orders"];
  const MANAGER = {"ovh-eu": "https://www.ovh.com/manager/#/dedicated/server/", "ovh-ca": "https://ca.ovh.com/manager/#/dedicated/server/",
                   "ovh-us": "https://us.ovhcloud.com/manager/#/dedicated/server/"};
  const ORDER_TAG = {delivered: "green", delivering: "cyan", checking: "amber", documentsRequested: "amber", notPaid: "amber",
                     cancelled: "gray", cancelling: "gray", unknown: "gray", expired: "gray"};
  const STATE_TAG = {ok: "green", error: "red", hacked: "red", hackedBlocked: "red"};
  const SVC_TAG = {ok: "", expired: "red", unPaid: "amber", pendingDebt: "amber", inCreation: "cyan", autorenewInProgress: "cyan"};
  const PAY_CN = {creditCard: T('信用卡'), paypal: "PayPal", bankAccount: T('银行账户'), fidelityAccount: T('积分'), ovhAccount: T('OVH 余额'),
                  credit: T('余额'), voucher: T('代金券'), sepaDirectDebit: "SEPA", mandate: T('授权扣款'), debtAccount: T('欠款账户'),
                  deposit: T('预付款'), refund: T('退款'), prepaidAccount: T('预付账户'), multiple: T('多种方式'), cheque: T('支票'),
                  transfer: T('银行转账'), bankTransfer: T('银行转账'), rupay: "RuPay", alipay: T('支付宝'), wechatpay: T('微信支付')};
  // OVH order / service statuses, translated in the browser too (fallback: the server's text, then the raw value)
  const ORDER_CN = {cancelled: T('已取消'), cancelling: T('取消中'), checking: T('核查中'), delivered: T('已交付'), delivering: T('交付中'),
                    documentsRequested: T('需提交资料'), notPaid: T('未付款'), unknown: T('未知'), expired: T('已过期')};
  const SVC_CN = {ok: T('正常'), expired: T('已过期'), inCreation: T('开通中'), unPaid: T('未付款'), pendingDebt: T('欠费待付'),
                  autorenewInProgress: T('自动续费中'), suspended: T('已暂停'), terminated: T('已终止'), toRenew: T('待续费')};
  const BWT_CN = {included: T('套餐内含'), standard: T('标准'), improved: T('增强'), premium: T('高级'), platinum: T('白金'), ultimate: T('至尊')};
  const ost = o => ORDER_CN[o.status] || o.statusCn || o.status || "";
  const $ = (s, el = document) => el.querySelector(s);
  const fmtTime = s => { if (!s) return "-"; const d = new Date(s); if (isNaN(d)) return esc(s);
    return fmtLocal(s, d.getFullYear() !== new Date().getFullYear()).slice(0, -3); };
  const fmtDate = s => s ? esc(String(s).slice(0, 10)) : "-";
  const daysTo = s => { const d = new Date(s); return isNaN(d) ? null : Math.ceil((d - Date.now()) / 864e5); };
  const tag = (cls, text, icon) => `<span class="tag ${cls || "gray"}">${icon ? ICON(icon) : ""}${esc(text)}</span>`;
  let polls = 0, timer = null, lastData = {};

  function notice(ep, d) {
    const denied = (d.denied || []).filter(x => x === "me" || WANT.includes(x));
    if (d.authInvalid) return `<div class="flash err">${ICON("warning")}<div><b>${esc(ep)}</b> ${T('的密钥无效或已失效（{0}）。请到', esc(d.errors?.me || ""))} <a href="/keys">${T('OVH 密钥')}</a> ${T('页重新获取并验证。')}</div></div>`;
    if (!denied.length) return "";
    const what = denied.filter(x => x !== "me").map(x => ({servers: T('查看服务器'), orders: T('查看订单')})[x]).join(T('、')) || T('读取账号');
    return `<div class="flash warn acct-denied">${ICON("key")}<div>
      <b>${esc(ep)}</b> ${T('当前的 Consumer Key 没有「{0}」权限（OVH 返回 403）。本站新增了两条只读权限', what)}
      <code>GET /dedicated/server</code>${T('、')}<code>GET /dedicated/server/*</code>${T('，旧令牌不包含它们，重新授权一次即可，下单功能不受影响。')}
      <div class="row" style="margin-top:10px">
        <form method="post" action="/keys/ck-request" class="inline"><input type="hidden" name="csrf" value="${esc(window.CSRF)}"><input type="hidden" name="endpoint" value="${esc(ep)}">
          <button class="primary sm">${ICON("safety-certificate")} ${T('用现有应用重新授权（推荐）')}</button></form>
        ${guide[ep] ? `<a class="btn sm" href="${esc(guide[ep].token)}" target="_blank" rel="noopener noreferrer">${ICON("link")} ${T('一键创建新令牌')}</a>` : ""}
      </div>
      <p class="small muted" style="margin:8px 0 0">${T('「重新授权」只换 Consumer Key：点击后到 OVH 页面登录授权，再回本站点「我已授权，验证并保存」。「创建新令牌」会生成一套新的 3 个值，需要全部粘贴到')} <a href="/keys">${T('OVH 密钥')}</a> ${T('页。')}</p>
    </div></div>`;
  }

  function serverCard(ep, s) {
    if (s.error) return `<div class="srv"><div class="srv-h"><div class="srv-n mono">${esc(s.name)}</div>${tag("red", T('读取失败'))}</div><p class="small muted wrap">${esc(s.error)}</p></div>`;
    const left = daysTo(s.expiration);
    const exp = s.expiration ? `${fmtDate(s.expiration)}${left != null && left >= 0 && left <= 15 ? ` <span class="tag amber">${left} ${T('天后')}</span>` : ""}${left != null && left < 0 ? ` <span class="tag red">${T('已过期')}</span>` : ""}` : "-";
    const cpu = s.cpu ? `${esc(s.cpu)}${s.cores ? ` <span class="muted">· ${s.cores}C${s.threads ? "/" + s.threads + "T" : ""}</span>` : ""}` : "-";
    const bw = s.bandwidthOut ? `${esc(s.bandwidthOut)}${s.bandwidthType ? ` <span class="muted">· ${esc(BWT_CN[s.bandwidthType] || s.bandwidthType)}</span>` : ""}` : (s.linkSpeed ? `${esc(s.linkSpeed)} ${T('Mbps 端口')}` : "-");
    const ips = [s.ip, ...(s.ips || [])].filter(Boolean);
    const svc = s.serviceStatus && s.serviceStatus !== "ok" ? " " + tag(SVC_TAG[s.serviceStatus], SVC_CN[s.serviceStatus] || s.serviceStatusCn) : "";
    const title = s.displayName && s.displayName !== s.name ? s.displayName : (s.reverse || "");
    return `<div class="srv">
      <div class="srv-h"><div class="srv-t"><div class="srv-n mono">${esc(s.name)}</div>${title ? `<div class="srv-d">${esc(title)}</div>` : ""}</div>
        <div class="row nowrap">${tag(STATE_TAG[s.state], s.stateCn, s.state === "ok" ? "check-circle" : "warning")}${svc}</div></div>
      <div class="srv-meta">${s.dcLabel ? `<span>${ICON("environment")}${esc(s.dcLabel)} <span class="mono muted">${esc(s.datacenter)}</span></span>` : ""}
        ${s.commercialRange ? `<span>${ICON("cloud-server")}${esc(s.commercialRange)}</span>` : ""}
        ${s.os ? `<span>${ICON("desktop")}<span class="mono">${esc(s.os)}</span></span>` : ""}</div>
      <dl class="srv-kv">
        <dt>CPU</dt><dd>${cpu}</dd>
        <dt>${T('内存')}</dt><dd>${esc(s.ram || "-")}</dd>
        <dt>${T('硬盘')}</dt><dd>${(s.disks || []).length ? s.disks.map(esc).join("<br>") : "-"}</dd>
        <dt>${T('带宽')}</dt><dd>${bw}</dd>
        <dt>IP</dt><dd class="mono">${ips.length ? ips.slice(0, 3).map(esc).join("<br>") + (s.ipCount > 3 ? ` <span class="muted">${T('等 {0} 个', s.ipCount)}</span>` : "") : "-"}</dd>
        <dt>${T('开通')}</dt><dd>${fmtDate(s.created)}</dd>
        <dt>${T('到期')}</dt><dd>${exp}</dd>
        <dt>${T('续费')}</dt><dd>${esc(s.renew || "-")}</dd>
      </dl>
      <div class="srv-f"><span class="muted small">${s.monitoring ? T('OVH 监控已开启') : T('OVH 监控未开启')}${s.rack ? T(' · 机柜 ') + esc(s.rack) : ""}</span>
        ${MANAGER[ep] ? `<a class="small" href="${MANAGER[ep]}${encodeURIComponent(s.name)}" target="_blank" rel="noopener noreferrer">${ICON("link")} ${T('控制台')}</a>` : ""}</div>
    </div>`;
  }

  const KIND_TAG = {invoice: "gray", debt: "amber", refund: "green", renew: "cyan", option: "gray"};
  function summaryCell(o) {
    const s = o.summary || {}, raw = o.items || [];
    const keepRaw = raw.length > 1 || ["invoice", "debt", "refund", "other"].includes(s.kind);
    const rawHtml = raw.length && keepRaw ? `<details class="rawitems"><summary>${T('原始明细 {0} 行', raw.length)}</summary>${raw.map(i =>
      `<div>${esc(i.description || "-")}${i.domain && i.domain !== "*" ? ` <span class="muted mono">${esc(i.domain)}</span>` : ""}${i.quantity > 1 ? ` <span class="muted">×${i.quantity}</span>` : ""}</div>`).join("")}</details>` : "";
    if (["invoice", "debt", "refund"].includes(s.kind))
      return `${tag(KIND_TAG[s.kind], s.kindCn)}${s.ref ? ` <span class="muted mono small">${esc(s.ref)}</span>` : ""}${rawHtml}`;
    const main = [s.model ? `<b>${esc(s.model)}</b>` : "", s.dc ? `<span title="${esc(s.dcLabel || "")}">${esc(s.dc.toUpperCase())}</span>` : "",
                  s.cpu ? `<span class="muted">${esc(s.cpu)}</span>` : ""].filter(Boolean).join(" · ");
    const cfg = [s.ram, ...(s.disks || []), s.bandwidth].filter(Boolean).map(esc).join(" · ");
    const tags = [s.kind && s.kind !== "server" ? tag(KIND_TAG[s.kind], s.kindCn) : "",
                  s.period ? `<span class="tag gray">${esc(s.period)}</span>` : "",
                  s.domain ? `<span class="tag gray mono" title="${T('服务器名')}">${esc(s.domain)}</span>` : "",
                  ...(s.extras || []).map(x => `<span class="muted small">${esc(x)}</span>`)].filter(Boolean).join(" ");
    if (!main && !cfg && !tags) return rawHtml || '<span class="muted">-</span>';
    return `${main ? `<div class="oi-main">${main}</div>` : ""}${cfg ? `<div class="oi-cfg small">${cfg}</div>` : ""}${tags ? `<div class="oi-tags">${tags}</div>` : ""}${rawHtml}`;
  }

  function orderRow(o) {
    const task = appOrders[String(o.orderId)];
    const items = summaryCell(o);
    const pay = o.paid ? `<span class="pay ok">${ICON("check-circle")}${T('已付款')}</span><div class="small muted">${esc(PAY_CN[o.payment?.type] || o.payment?.type || "")} ${fmtTime(o.payment?.date)}</div>`
      : (o.status === "cancelled" ? `<span class="muted">-</span>` : `<span class="pay no">${T('未付款')}</span>${o.expiration ? `<div class="small muted">${T('有效期至')} ${fmtTime(o.expiration)}</div>` : ""}`);
    return `<tr id="ovh-order-${esc(o.orderId)}">
      <td class="mono small">${fmtTime(o.date)}</td>
      <td><code>${esc(o.orderId)}</code>${task ? ` <a class="tag accent" href="/tasks/${task}" title="${T('本站任务 #{0} 下的单', task)}">${T('本站 #')}${task}</a>` : ""}</td>
      <td class="wrap items">${items || '<span class="muted">-</span>'}</td>
      <td class="num price">${esc(o.amountText || "-")}${o.amountCny ? `<span class="cny">${esc(o.amountCny)}</span>` : ""}</td>
      <td>${tag(ORDER_TAG[o.status], ost(o))}</td>
      <td class="small">${pay}</td>
      <td class="actions">${o.url ? `<a class="btn sm ghost" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${ICON("link")} ${o.paid || o.status === "cancelled" ? T('查看') : T('付款/查看')}</a>` : ""}</td>
    </tr>`;
  }

  const errBox = (msg) => `<div class="empty">${ICON("warning")}${T('读取失败：')}${esc(msg)}<br><span class="small">${T('稍后点右上角「刷新」重试')}</span></div>`;

  function render(entry) {
    const ep = entry.endpoint, sec = document.querySelector(`.acct[data-ep="${ep}"]`);
    if (!sec || !entry.data) return;
    const d = entry.data, denied = d.denied || [];
    $(".acct-notice", sec).innerHTML = notice(ep, d);
    const srvEl = $('[data-slot="servers"]', sec), ordEl = $('[data-slot="orders"]', sec);
    // servers
    if (!srvEl) { /* page without the servers card */ }
    else if (d.servers) {
      $('[data-slot="srv-count"]', sec).textContent = d.servers.total ? `${T('共 {0} 台', d.servers.total)}${d.servers.total > d.servers.shown ? `${T('，显示 {0} 台', d.servers.shown)}` : ""}` : "";
      srvEl.innerHTML = d.servers.rows.length ? d.servers.rows.map(s => serverCard(ep, s)).join("")
        : `<div class="empty">${ICON("cloud-server")}${T('这个账号下还没有独立服务器')}</div>`;
    } else {
      $('[data-slot="srv-count"]', sec).textContent = "";
      srvEl.innerHTML = denied.includes("servers") || d.authInvalid ? `<div class="empty">${ICON("key")}${d.authInvalid ? T('密钥无效，无法读取') : T('缺少查看服务器的权限，见上方提示')}</div>` : errBox(d.errors?.servers || d.errors?.me || T('未知错误'));
    }
    // orders
    if (!ordEl) { /* page without the orders card */ }
    else if (d.orders) {
      $('[data-slot="ord-count"]', sec).textContent = d.orders.total ? `${T('近 {0} 天共 {1} 单', d.orders.days, d.orders.total)}${d.orders.total > d.orders.shown ? `${T('，显示最近 {0} 单', d.orders.shown)}` : ""}` : "";
      ordEl.innerHTML = d.orders.rows.length ? `<table class="t acct-t"><thead><tr><th>${T('日期')}</th><th>${T('订单号')}</th><th>${T('内容')}</th><th class="num">${T('金额（含税）')}</th><th>${T('状态')}</th><th>${T('付款')}</th><th></th></tr></thead><tbody>${d.orders.rows.map(orderRow).join("")}</tbody></table>`
        : `<div class="empty">${ICON("file-text")}${T('近 {0} 天没有订单', d.orders.days)}</div>`;
      for (const o of d.orders.rows) {   // link 本站记录 rows to the live OVH status
        document.querySelectorAll(`[data-oid="${CSS.escape(String(o.orderId))}"] .acct-st`).forEach(el => {
          el.innerHTML = `<a href="#ovh-order-${esc(o.orderId)}" title="${T('OVH 账号中的当前状态')}">${tag(ORDER_TAG[o.status], ost(o))}</a>`;
        });
      }
    } else {
      $('[data-slot="ord-count"]', sec).textContent = "";
      ordEl.innerHTML = denied.includes("orders") || d.authInvalid ? `<div class="empty">${ICON("key")}${d.authInvalid ? T('密钥无效，无法读取') : T('缺少查看订单的权限，见上方提示')}</div>` : errBox(d.errors?.orders || d.errors?.me || T('未知错误'));
    }
    sec.classList.remove("loading");
  }

  function setAge(entries) {
    const loading = entries.some(e => e.loading), ages = entries.filter(e => e.age != null).map(e => e.age);
    const btn = $("#acctRefresh"), age = $("#acctAge");
    btn.disabled = loading; btn.classList.toggle("spinning", loading);
    if (loading) age.textContent = ages.length ? T('正在刷新…') : T('正在从 OVH 读取…');
    else if (ages.length) { const a = Math.max(...ages); age.textContent = a < 60 ? T('刚刚更新') : `${Math.round(a / 60)} ${T('分钟前更新')}`; }
  }

  async function load(refresh = false) {
    clearTimeout(timer);
    let res;
    try {
      const r = await fetch("/api/account" + (refresh ? "?refresh=1" : ""), {headers: {Accept: "application/json"}});
      if (!r.ok) throw new Error("HTTP " + r.status);
      res = await r.json();
    } catch (e) {
      $("#acctAge").textContent = T('读取失败，稍后重试');
      timer = setTimeout(load, 5000); return;
    }
    for (const e of res.endpoints) {
      if (e.data && e.fetchedAt !== lastData[e.endpoint]) { render(e); lastData[e.endpoint] = e.fetchedAt; }
    }
    setAge(res.endpoints);
    if (res.endpoints.some(e => e.loading) && ++polls < 120) timer = setTimeout(load, polls < 10 ? 1200 : 2500);
    else polls = 0;
  }

  $("#acctRefresh").addEventListener("click", () => { polls = 0; load(true); });
  load();
})();
