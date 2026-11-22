# 安全审查与加固（v1.4，v1.5 更新）

范围：整个应用（FastAPI 后端、Jinja 模板与前端 JS、后台抢购/跟踪任务、通知、Docker 部署）。
结论：未发现可直接远程利用的高危漏洞；下面列出的问题均已修复，剩余风险见最后一节。最大的剩余风险是**公网 HTTP 明文访问**。

## 1. 已发现并修复

| # | 问题 | 风险 | 修复 |
|---|------|------|------|
| 1 | Webhook 可填任意 URL，服务器会向其 POST | **SSRF**：可探测/访问内网、Docker 网络、云元数据 `169.254.169.254` | `app/netguard.py`：只允许 http/https、禁止 userinfo/localhost/.local/.internal；解析后**所有** IP 必须是公网（拒绝私网、回环、链路本地、保留、组播、IPv4 映射 IPv6、各家元数据地址）；发送时**固定解析出的 IP**（Host 头 + SNI）防 DNS rebinding，并禁止跟随重定向。保存与发送时都校验 |
| 2 | Telegram 请求异常信息里带 `https://api.telegram.org/bot<TOKEN>/…` | 机器人 Token 进入日志/页面提示 | 通知错误只保留异常类型；统一 `_scrub()` 去掉 Token、Webhook 地址和 `bot\d+:…` 形式的串 |
| 3 | uvicorn `--forwarded-allow-ips "*"` | 任何客户端可伪造 `X-Forwarded-For`，绕过按 IP 的登录限速、伪造审计日志 IP | 改为 `FORWARDED_ALLOW_IPS=127.0.0.1`（可在 .env 覆盖为反代的地址段） |
| 4 | 登录时用户不存在直接返回 | 按响应时间**枚举用户名** | 不存在的用户也做一次 bcrypt 校验（`DUMMY_HASH`），耗时一致；错误提示统一 |
| 5 | 登录/注册（v1.5 已删除注册）/发测试通知/刷新账号订单无频率限制 | 暴力破解、被用来刷 Telegram/Webhook、刷 OVH API 配额 | 登录失败：每 IP 10 次/10 分钟、每用户名 8 次/15 分钟（达到后即使密码正确也拒绝）；注册 5 次/小时/IP；测试通知 6 次/分钟；账号订单强制刷新 10 次/10 分钟；`/api/price` 240 次/分钟；授权链接 10 次/10 分钟；订单「立即检查」6 次/分钟 |
| 6 | 密码最少 8 位，无复杂度 | 弱口令 | ≥10 位、至少两类字符、拒绝常见口令（含 `Password123!` 这类「常见词+数字」变体）、拒绝包含用户名、拒绝重复字符 |
| 7 | 无法改密码、无法踢掉其他设备；会话只有 14 天绝对过期 | 密码/会话泄露后无法止损 | 新增「账号与安全」页：改密码、退出所有设备。会话绑定 `session_epoch`，改密码/退出所有设备后旧 Cookie 全部失效；3 天无操作自动过期；登录时整体替换会话并轮换 CSRF Token（防会话固定） |
| 8 | 模板里有 `onclick="return confirm(...)"` 等内联脚本 | 无法启用严格 CSP；一旦出现 XSS 没有第二道防线 | 去掉全部内联事件，改为 `data-confirm`；启用**基于 nonce 的严格 CSP**（`script-src 'self' 'nonce-…'`，无 `unsafe-inline`/`unsafe-eval`） |
| 9 | 缺少安全响应头 | 点击劫持、MIME 嗅探、Referer 泄露 | `Content-Security-Policy`（含 `frame-ancestors 'none'`、`form-action 'self'`、`base-uri 'none'`、`object-src 'none'`）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: same-origin`、`Permissions-Policy`、`Cross-Origin-Opener-Policy`、动态页面 `Cache-Control: no-store`；启用 HTTPS 后加 HSTS |
| 10 | 只有 CSRF Token，没有来源校验 | 纵深防御不足 | 所有 POST/PUT/PATCH/DELETE 额外检查 `Origin`/`Referer` 必须与 Host 同源，否则 403（`null` Origin 也拒绝）。所有 `/api/*` 都是只读 GET（`/api/account?refresh=1` 只触发只读拉取，且限速），不存在可被 CSRF 利用的 JSON 写接口 |
| 11 | 停止任务后按 `Referer` 跳转 | 开放跳转的雏形 | 只允许跳回 `/` 或任务页 |
| 12 | SQLite 数据库、缓存文件权限默认 644 | 同机其他用户可读（含加密后的密钥） | 启动时 `data/` 设为 700、`app.db*`/缓存 600；容器入口 `umask 077` |
| 13 | 无审计记录 | 出事无法追溯 | 新增审计日志：登录/失败、注册、改密码、退出所有设备、保存/删除密钥、创建/启动/删除任务等；用户在「账号与安全」看自己的，管理员在「系统管理」看全部；自动保留最近 2 万条 |
| 14 | `/api/price` 等参数校验依赖 `assert` | `python -O` 下校验失效 | 改为显式校验 |

## 1.5 v1.5：单一所有者模式

| 变更 | 原因 |
|---|---|
| **删除公开注册**：没有 `/register` 路由、没有注册链接、没有「开放注册」开关；`/admin/settings`、`/admin/users/*/toggle` 一并删除 | 本项目是一人一实例的自托管面板。开放注册意味着任何人都能用你的服务器、消耗 OVH API 配额，并扩大攻击面 |
| **所有者账号只能在服务器上用命令行创建**：`python -m app.cli create-admin`（`install.sh` 安装时交互调用，或 `ovh-sniper create-admin`）。仅当数据库里还没有任何账号时才能创建；之后只能 `reset-password` | 没有选「首次访问设置页」：应用默认直接以 HTTP 监听 `0.0.0.0`，部署完成到你打开页面之间存在**竞争窗口**，任何扫描器先访问设置页就能接管实例。命令行方式要求 SSH 登录服务器，等于复用服务器本身的认证 |
| 密码通过 `getpass` 输入（不回显、不进 shell 历史），或 `--password-stdin` 从管道读取（不出现在进程列表）；同样执行密码策略 | 避免密码出现在 `ps`、`~/.bash_history`、`docker inspect` 里 |
| `reset-password` 会递增 `session_epoch`，所有已登录设备立即失效；写审计日志（IP 记为 `cli`） | 忘记密码 / 怀疑泄露时可以在服务器上止损 |
| 代码里仍保留按 `user_id` 的数据隔离和 IDOR 检查 | 纵深防御；升级前已有多个账号的实例不会互相看到数据（`list-users` 可查看） |
| 实时日志改为 **SSE**（`/api/tasks/{id}/stream`）：同源、需要登录、只读，按任务属主校验；每个连接 25 秒后自动结束并由浏览器重连，数据库查询在线程里执行且不持有长事务；`Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no` | 满足 `connect-src 'self'`，不放宽 CSP；连接不会无限占用；反代不会缓冲 |
| uvicorn `--timeout-graceful-shutdown 10` | 长连接不会拖住重启（compose 的 `stop_grace_period: 45s` 仍留给正在结账的任务） |
| 发布仓库：`.gitignore` 排除 `.env`、`data/`、备份；测试改用随机生成的演示账号（`/tmp/ovh-sniper-mock-cred.json`），测试数据里的 IP 全部使用文档保留地址段（203.0.113.0/24、198.51.100.0/24、2001:db8::/32） | 不让密钥、个人数据或真实 IP 进入 git 历史 |

## 2. 审查过、确认没有问题的部分

- **IDOR**：任务、日志、订单、订单详情/检查、OVH 密钥、通知设置全部按 `user_id` 过滤，访问他人资源返回 404（有自动化测试）。管理员按设计可以查看所有任务。
- **XSS**：Jinja2 自动转义开启；前端所有 `innerHTML` 拼接都经过 `esc()`；OVH 返回的数据、日志、订单链接都转义后输出；外链统一 `rel="noopener noreferrer"`。Telegram 消息使用 HTML 模式且逐行转义。
- **SQL 注入**：全部走 SQLAlchemy 参数化查询；迁移只用固定列名。
- **密钥存储**：OVH AK/AS/CK、Telegram Token、Webhook 用 Fernet（密钥由 `SECRET_KEY` 派生）加密存库；页面只显示掩码；日志、错误提示、通知内容里都不出现。
- **会话 Cookie**：`HttpOnly`、`SameSite=Lax`、签名防篡改，`COOKIE_SECURE=1` 时加 `Secure`。Cookie 内容只有用户 ID、CSRF Token、会话代数和时间戳，没有敏感数据（签名但不加密）。
- **下单安全**：真实下单前强制价格上限检查；结账结果不确定时停止并要求人工确认，不会重复下单；订单跟踪只用 GET。
- **Docker**：应用以非 root 用户（uid 1000）运行，入口脚本只用 root 修正挂载目录的属主后立即降权。
- **依赖**：`pip-audit -r requirements.txt` → 无已知漏洞（2026-11-20）。

## 3. 剩余风险与建议（按优先级）

1. **公网 HTTP 明文（最重要）**：一键安装后默认 `0.0.0.0:端口` 直接 HTTP，登录密码和会话 Cookie 在网络上明文传输，可能被中间人窃取。建议任选其一：
   - **域名 + Caddy（推荐）**：Caddy 自动申请证书；443 已被其他程序占用时可以监听其他端口（如 8443，证书用 DNS 验证）。然后在 `.env` 设置 `BIND_ADDR=127.0.0.1`、`COOKIE_SECURE=1`，并把 `FORWARDED_ALLOW_IPS` 设为 Docker 网关地址段（如 `172.16.0.0/12`），这样按 IP 的限速才能看到真实客户端 IP。
     ```
     # /etc/caddy/Caddyfile（示例，8443 端口，使用 DNS 验证申请证书）
     sniper.example.com:8443 {
         reverse_proxy 127.0.0.1:8000
     }
     ```
   - **Cloudflare Tunnel**：`cloudflared tunnel --url http://127.0.0.1:8000`（或绑定域名的固定隧道）。不需要开放任何端口，自带 HTTPS；同样设置 `COOKIE_SECURE=1` 和 `BIND_ADDR=127.0.0.1`。
2. **服务器本身的安全 = 面板的安全**：所有者账号由 SSH 创建和重置，请给服务器使用 SSH 密钥登录并禁用密码登录（或至少使用强密码 + fail2ban）。
3. **限速状态在内存里**，重启后清零；单进程部署可以接受。多进程或多实例时需要换成 Redis。
4. **DNS rebinding**：Webhook 发送时已固定 IP，只剩「解析→连接」之间的极短窗口，且连接目标就是校验过的 IP，风险很低。
5. **面板密码**：所有者账号可以用你的 OVH 密钥下单付款，请使用强密码；怀疑泄露时在服务器上运行 `ovh-sniper reset-password`（所有设备立即下线），并到 OVH 控制台撤销 Consumer Key。
6. **`SECRET_KEY` 是总钥匙**：它同时用于签名会话和加密密钥，泄露后需要更换 `SECRET_KEY`。更换后所有已保存的密钥都要重新填写，所有会话也会失效。请备份好 `.env`，不要提交到任何仓库。
7. **OVH 令牌权限**：令牌只授予需要的接口；不用时在 OVH 控制台撤销。查看服务器实际配置需要 `GET /dedicated/server*` 权限。

## 4. 自动化测试

- `tests/v14_test.py`（v1.5 起含单一所有者检查：`/register` 404、CLI 只能在无账号时创建）：通知分类/开关/去重/Token 不泄露、SSRF 拦截（含 DNS 指向内网）、价格计算、订单跟踪（付款→交付→中奖）、安全响应头与 nonce、密码策略、IDOR、CSRF/跨站、会话失效（退出所有设备/改密码）、登录与测试通知限速
- `tests/ui_v14.py`：浏览器里检查型号选择器问题、开关、价格面板、通知页、订单详情，以及**页面上无 CSP 违规/JS 报错**（浅色+深色）
- `tests/mock_order_test.py`：模拟 OVH 的下单全流程（含 `ONLY_UNPAID=1`：只下单不付款）
