# Changelog · 更新记录

## v2.1.0
- **Prettier installer output.** `install.sh` shows a StockHawk banner with the version, numbered steps `[1/9]…[9/9]` with spinners and ✔ / ✖ / ! status, and a summary box (panel URL, directory, log, commands, HTTPS tip, GitHub and www.7919.net). Noisy apt / Docker / git / build output goes to `/opt/ovh-sniper/install.log` (mode 600); on failure the last 15 lines and the log path are printed. Re-running on an existing install shows "updating" and keeps `.env` / `data/`. Plain text, no colours or spinners when the output is not a terminal, `NO_COLOR` is set or `TERM=dumb`; prompts still read from `/dev/tty` under `curl | sudo bash`. All env vars and behaviour unchanged.
- **安装脚本输出美化**：横幅 + 版本号、带进度动画的步骤 `[1/9]`、✔ / ✖ / ! 状态和最后的汇总框；apt / Docker / 构建等详细输出写入 `/opt/ovh-sniper/install.log`，失败时打印最后几行和日志路径；非终端、`NO_COLOR` 或 `TERM=dumb` 时自动改为纯文本。环境变量和行为不变。

## v2.0.1
- Fix: `ovh-sniper reset-password` / `create-admin` failed with "an interactive terminal is required" because the TTY check ran inside a subshell. / 修复 `ovh-sniper reset-password`、`create-admin` 报“需要交互式终端”的问题。

## v2.0 · StockHawk for OVH
- **Renamed to StockHawk for OVH** (repository `stockhawk-ovh`), new logo (hawk head with radar eye) and favicon set in `docs/brand/` and `app/static/brand/`. Unofficial-project notice in the panel footer, login page and READMEs. CLI command, install directory, container and env names are unchanged, so `ovh-sniper update` upgrades in place.
- **New-task picker = Live stock table.** Same filters, search, catalog filter, in-stock toggle, stock badges, readable names, config and price. Tick rows to batch-select several machines (model + config + datacenter); rows of the same config merge into one item with several datacenters; "whole model" adds an any-config / any-datacenter item. A *Selected* tray shows the priority numbers, reorders by drag or ↑/↓, and edits each item's RAM / disk / bandwidth (or "any") and datacenter order. Deep links from Live stock (`plan,type,dc,memory,storage,endpoint`, or repeated `item=plan|type|memory|storage|dc1+dc2`) pre-fill the task. Tasks store per-item datacenters (`plan_items`); older tasks load unchanged.
- **Telegram restock card.** HTML message without emoji: bold title, headline, Product / CPU / RAM / Storage / Datacenter (city) / Availability / Price (≈CNY) / Setup fee, detection time, and inline buttons to the OVH express order page for your subsidiary (plan, datacenter and region pre-selected) plus the product page. When the stock goes, the same message is edited: body struck through and "Sold out at <time>", buttons removed; a restock sends a new message. Bilingual per the notification language. Optional `ORDER_LINK_AFF` appends your referral query to order links. The notification test button sends a sample card.
- **OVH order statuses in your language.** Order / service status badges and payment methods from your OVH account (Unpaid, Delivering, Checking, `debtAccount`…) were fetched in worker threads and came out in English even with the Chinese UI; they now follow the reader's language (full OVH order-status enum, payment types such as 欠款账户 / Debt account, service statuses, bandwidth types; unknown values shown raw). Telegram order texts re-render the status in the notification language. Test: `tests/order_status_i18n_test.py`.
- Tests: `tests/task_items_test.py`, `tests/task_picker_ui.py`, `tests/stock_card_test.py` (mocked Bot API: send + edit), `tests/tg_card_preview.py` (renders a preview image).
- **更名为 StockHawk for OVH**，新 Logo；页脚、登录页和 README 注明「非 OVHcloud 官方项目，与 OVH 无关联」。**新建任务的选机界面与实时库存相同**，可勾选多台机器批量加入，按勾选顺序定优先级（可拖动/上下移），每台机器单独设置配置与机房。**Telegram 补货卡片**：无 emoji 的 HTML 消息，带 OVH 下单按钮；售罄后自动把原消息改为删除线并注明「已被抢完」，再补货发新消息。

## v1.8
- **Behind Cloudflare (orange cloud):** `TRUST_CLOUDFLARE=1` trusts CF-Connecting-IP / X-Forwarded-Proto / CF-Visitor only from Cloudflare's IP ranges (`CF_IPS` to override): real client IP for login throttling and the audit log, HTTPS-aware redirects and same-origin check, and per-request Secure cookies for HTTPS visitors while direct `http://IP:8000` keeps working. Optional `ALLOWED_ORIGINS`. README: HTTPS option 3 (Cloudflare proxy + Origin Rule, works when 443 is busy).
- **New order mode: Notify only (仅提醒).** Watches public availability and sends a Telegram alert when matching stock appears; never builds carts and never touches the OVH account (works without API keys). Alerts fire when stock appears, again after it sells out and comes back, with a per-task cooldown (default 10 min, 0 = none). Every matching plan alerts, not only the top-priority one.
  - Telegram must be set up (token + chat ID, stock alerts on): the card shows an inline warning with a link to Notifications, saving/starting is blocked in the browser and on the server.
  - Fast lane is hidden for notify-only and disabled for preview.
- Notifications page: @BotFather and @userinfobot are t.me links (open the Telegram app).
- Tests: `tests/notify_only_test.py`, `tests/notify_only_ui.py`.
- **新下单方式「仅提醒」**：只监控公开库存，有货发 Telegram 提醒，不建购物车、不碰 OVH 账号（无需密钥）；未配置 Telegram 时卡片内提示并禁止保存/启动；补货即提醒、售罄后再补货会再提醒，可设冷却时间。通知页的 @BotFather / @userinfobot 可直接点击。

## v1.7
- **Bilingual UI: English / 中文, English by default.** EN / 中 switch in the top right, saved per user (database + cookie); never follows the browser's Accept-Language. `DEFAULT_LANG` in `.env` sets the default.
- Telegram / webhook notifications have their own language setting (default: follow the panel language).
- Flash messages, task logs, CLI and the install / manage / update / uninstall scripts are bilingual (`OVH_SNIPER_LANG`).
- New tests: `tests/i18n_test.py` (browser CJK scan of every page in English), `tests/i18n_unit_test.py`; catalog checker `tools_i18n_extract.py`.
- English README (Chinese in README.zh-CN.md) with English screenshots.
- **双语界面（默认英文）**：右上角 EN / 中 切换，按用户保存；Telegram 通知可单独设语言；命令行和管理脚本双语。

## v1.6
- **全球机房库存地图**（控制台）：世界地图 + 欧洲放大图，标出 OVH 独服机房（BHS、TOR、VIN、HIL、GRA、RBX、SBG、PAR、LON、FRA、WAW、SGP、SYD、YNM），按库存着色：绿 = 现货充足（1H-high）、黄 = 少量现货（1H-low / 24H）、蓝 = 可订·等交付（72H 及更长）、红 = 无货、灰 = 未知 / 未监控；有货机房呼吸光圈，悬停 / 点击看每个型号的库存；跟随浅色 / 深色主题，遵守「减少动态效果」设置
  - 范围：你的任务型号（运行中的优先）→ 没有任务时用共享轮询正在监控的型号 → 都没有时用 RISE-1 / ADVANCE-1 示例；数据来自共享轮询缓存，每 15 秒刷新，不额外请求 OVH
  - 新接口 `GET /api/stock/map`（需登录）；颜色映射集中在 `app/dcmap.py`
  - 地图底图为 Natural Earth 陆地数据（公共领域），离线生成为 `app/static/worldmap.svg`（`tools/gen_worldmap.py`），不加载任何外部瓦片或 CDN
- **修复：库存接口里出现、但官方目录买不到的配置会让任务停止**。例：KS-LE-3（`27skle03-v1`）库存接口显示 `ram-64g-ddr4-ecc-2666` 在 FRA/GRA 有货，但 IE 等所有子公司的目录和购物车选项里只有 `ram-32g-ddr4-ecc-2666-27skle03-v1`。现在：
  - 这类配置标记为「不可下单」（实时库存、控制台、日志），不会再被「允许非基础配置」选中，也不会显示错误价格
  - 任务选了这类配置时不再停止，而是给出 WARN 说明（目录里可选的是什么）并继续监控
  - 有货但不匹配时，日志 `NO_MATCH` 和每分钟心跳会写明原因（不是所选内存 / 硬盘、非基础配置未开启、不在所选机房、目录无此配件）
  - 配件匹配更稳：忽略 planCode 后缀和 DDR 代数差异（`ram-64g-ecc-2666` ⇄ `ram-64g-ddr4-ecc-2666-<plan>`），仍然绝不悄悄换成默认配置

## v1.5
- **单一所有者模式**：删除公开注册（无 `/register`、无注册开关）；所有者账号在服务器上用 `python -m app.cli create-admin` 创建（`install.sh` 交互调用），`reset-password` 重置密码并踢下线所有设备
- **实时日志**改为 SSE 推送（断线自动重连 / 回退轮询），新日志 1–2 秒内出现；状态行显示第几次轮询、多久之前、耗时、是否有匹配库存；心跳改为每 60 秒一次
- **进行中的任务**：任务页、控制台顶部显示运行中的任务（型号、模式、轮询、心跳），一键停止
- **可读的型号和配置**：显示通用型号名 + CPU（`KS-B · Intel Xeon E5-1620v2`），planCode 作为小标签；内存 / 硬盘 / 带宽显示为 `32 GB ECC 1333`、`1×120 GB SSD（无 RAID）`、`500 Mbps`（任务列表、任务详情、进行中的任务、日志、通知、订单）
- **我的服务器**移到控制台；订单记录只保留订单
- **账号订单整理**：每单一行「型号 · 机房 · CPU」+ 一行配置 + 周期 / 服务器名标签；去掉重复的 rental 行和内部 `*001.001` 编号；账单 / 欠款支付 / 退款显示为简短标签；原始明细可展开
- 人民币显示补全：费用预估每个价格（含首月租金）、首期上限提示、账号订单、订单详情、通知
- 一键安装 / 更新 / 卸载脚本，`ovh-sniper` 管理命令（备份、恢复、重置密码…）
- 停用的旧 planCode（如 `24rise01`）自动对应到新版本目录，订单详情能显示官方规格

## v1.4
- 三种下单方式（只预览 / 只下单不付款 / 下单并自动付款）；费用预估（月付 / 12 / 24 个月预付、增值税、人民币）
- 分类通知（有货 / 提交订单 / 付款 / 交付 / 任务提醒），每类单独开关和测试
- 交付跟踪 + 实际配置与官网规格对比（抽到更高配置时提示「中奖」）
- 安全加固：严格 CSP（nonce）、同源校验、SSRF 防护、限速、密码策略、会话失效、审计日志

## v1.3
- 用保存的密钥只读拉取 OVH 账号已有的服务器和订单

## v1.2
- 界面重做（浅色 / 深色主题，跟随系统）；下单流程审计与修复（多型号优先级、结账前复查、结账中重启不自动恢复…）

## v1.1
- 型号多选、实时库存列表、Ant Design Icons（MIT，本地托管）
