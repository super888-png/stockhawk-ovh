import datetime as dt

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    # bumped on password change / "log out everywhere": every session cookie carrying an older epoch is rejected
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    lang: Mapped[str] = mapped_column(String(8), default="")   # "" = DEFAULT_LANG (en) | "en" | "zh"
    tasks = relationship("Task", back_populates="user", cascade="all, delete-orphan")


class AppSetting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Credential(Base):
    """One set of OVH API keys per user per endpoint. All secrets Fernet-encrypted."""
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "endpoint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(16))
    app_key_enc: Mapped[str] = mapped_column(Text, default="")
    app_secret_enc: Mapped[str] = mapped_column(Text, default="")
    consumer_key_enc: Mapped[str] = mapped_column(Text, default="")
    pending_ck_enc: Mapped[str] = mapped_column(Text, default="")
    nichandle: Mapped[str] = mapped_column(String(64), default="")
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NotifySetting(Base):
    __tablename__ = "notify"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    telegram_token_enc: Mapped[str] = mapped_column(Text, default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="")
    webhook_url_enc: Mapped[str] = mapped_column(Text, default="")
    notify_stock: Mapped[bool] = mapped_column(Boolean, default=True)      # 有货通知
    notify_order: Mapped[bool] = mapped_column(Boolean, default=True)      # 提交订单通知
    notify_paid: Mapped[bool] = mapped_column(Boolean, default=True)       # 付款通知
    notify_delivered: Mapped[bool] = mapped_column(Boolean, default=True)  # 交付通知
    notify_system: Mapped[bool] = mapped_column(Boolean, default=True)     # 异常 / 任务结束 / 密钥失效
    stock_debounce: Mapped[int] = mapped_column(Integer, default=600)      # s between 有货通知 for the same plan
    lang: Mapped[str] = mapped_column(String(8), default="")               # Telegram/webhook language: "" = panel language


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    endpoint: Mapped[str] = mapped_column(String(16), default="ovh-eu")
    subsidiary: Mapped[str] = mapped_column(String(8), default="IE")
    catalog_type: Mapped[str] = mapped_column(String(32), default="eco")  # eco | baremetalServers
    plan_code: Mapped[str] = mapped_column(String(80))
    plan_label: Mapped[str] = mapped_column(String(200), default="")
    # Multi-plan: csv of planCodes in priority order (plan_code = first one, kept for compatibility)
    plan_codes: Mapped[str] = mapped_column(Text, default="")
    # JSON {planCode: {"label":..., "memory":..., "storage":..., "bandwidth":...}}
    plan_configs: Mapped[str] = mapped_column(Text, default="")
    datacenters: Mapped[str] = mapped_column(String(200), default="")  # csv, priority order; empty = any
    # v1.9: ordered selection list [{planCode,type,label,memory,storage,bandwidth,dcs:[...]}]; the same planCode may
    # appear several times (different config / datacenters). dcs = [] -> any datacenter. Empty column = legacy rows
    # (plan_codes + plan_configs + task-wide datacenters), which keep working unchanged.
    plan_items: Mapped[str] = mapped_column(Text, default="")
    memory: Mapped[str] = mapped_column(String(120), default="")      # availability fragment; empty = any
    storage: Mapped[str] = mapped_column(String(160), default="")
    bandwidth: Mapped[str] = mapped_column(String(120), default="")   # addon planCode; empty = catalog default
    extra_addons: Mapped[str] = mapped_column(Text, default="")       # csv of optional addon planCodes
    any_config: Mapped[bool] = mapped_column(Boolean, default=False)  # allow non-default ram/disk when not pinned
    os_code: Mapped[str] = mapped_column(String(64), default="none_64.en")
    duration: Mapped[str] = mapped_column(String(8), default="P1M")
    pricing_mode: Mapped[str] = mapped_column(String(32), default="default")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    max_orders: Mapped[int] = mapped_column(Integer, default=1)
    price_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # first invoice incl. tax
    vat_rate: Mapped[float] = mapped_column(Float, default=1.23)
    poll_interval: Mapped[float] = mapped_column(Float, default=10.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_pay: Mapped[bool] = mapped_column(Boolean, default=False)
    prebuild: Mapped[bool] = mapped_column(Boolean, default=False)
    # 仅提醒 / notify only: watch public availability and send Telegram alerts; never touches the OVH account
    notify_only: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_cooldown: Mapped[int] = mapped_column(Integer, default=10)  # minutes between alerts for the same plan
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="stopped")
    status_text: Mapped[str] = mapped_column(Text, default="")
    orders_done: Mapped[int] = mapped_column(Integer, default=0)
    polls: Mapped[int] = mapped_column(Integer, default=0)
    last_poll_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    user = relationship("User", back_populates="tasks")

    @property
    def pay_mode(self) -> str:
        """notify | dry | order | pay"""
        return "notify" if self.notify_only else ("dry" if self.dry_run else ("pay" if self.auto_pay else "order"))


class TaskLog(Base):
    __tablename__ = "task_logs"
    __table_args__ = (Index("ix_log_task_id", "task_id", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    marker: Mapped[str] = mapped_column(String(32), default="INFO")
    msg: Mapped[str] = mapped_column(Text, default="")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(16), default="order")  # order | preview | pending
    order_id: Mapped[str] = mapped_column(String(40), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    plan_code: Mapped[str] = mapped_column(String(80), default="")
    dc: Mapped[str] = mapped_column(String(16), default="")
    memory: Mapped[str] = mapped_column(String(120), default="")
    storage: Mapped[str] = mapped_column(String(160), default="")
    price_with_tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    # v1.4 delivery tracking (real orders only)
    endpoint: Mapped[str] = mapped_column(String(16), default="")
    ovh_status: Mapped[str] = mapped_column(String(32), default="")
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    server_name: Mapped[str] = mapped_column(String(120), default="")
    spec_json: Mapped[str] = mapped_column(Text, default="")           # {"official":…, "actual":…, "upgrades":[…]}
    track_state: Mapped[str] = mapped_column(String(16), default="")   # "" | tracking | delivered | timeout | cancelled
    next_check: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    notified: Mapped[str] = mapped_column(String(64), default="")      # csv: paid,delivered


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_user", "user_id", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


def task_plans(t: "Task") -> list[dict]:
    """Ordered list of {planCode,label,memory,storage,bandwidth[,type,dcs]} for a task.
    `dcs` is only present for v1.9 selection items (list, [] = any); legacy rows use the task-wide datacenters."""
    import json
    try:
        items = json.loads(t.plan_items or "[]")
    except Exception:
        items = []
    if isinstance(items, list) and items:
        out = []
        for x in items:
            if not isinstance(x, dict) or not x.get("planCode"):
                continue
            out.append({"planCode": str(x["planCode"]), "label": x.get("label") or "", "memory": x.get("memory") or "",
                        "storage": x.get("storage") or "", "bandwidth": x.get("bandwidth") or "",
                        "type": x.get("type") or "", "dcs": [d for d in (x.get("dcs") or []) if isinstance(d, str)]})
        if out:
            return out
    codes = [c for c in (t.plan_codes or "").split(",") if c] or ([t.plan_code] if t.plan_code else [])
    try:
        cfg = json.loads(t.plan_configs or "{}")
    except Exception:
        cfg = {}
    out = []
    for i, pc in enumerate(codes):
        c = cfg.get(pc) or {}
        if not c and i == 0:
            c = {"label": t.plan_label, "memory": t.memory, "storage": t.storage, "bandwidth": t.bandwidth}
        out.append({"planCode": pc, "label": c.get("label") or "", "memory": c.get("memory") or "",
                    "storage": c.get("storage") or "", "bandwidth": c.get("bandwidth") or ""})
    return out
