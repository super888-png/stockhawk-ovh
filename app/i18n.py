"""Bilingual UI (English default, Simplified Chinese optional).

Message ids are the original Chinese strings; app/i18n_en.json maps them to English.
  T("已轮询 {0} 次", n)      -> "Polled 1 time(s)" in English, the Chinese text in zh
  Jinja: {{ _("控制台") }}    JS: T("…", {name: v}) with the catalog injected by base.html
The current language lives in a context variable: set per request from the `lang` cookie (mirrors User.lang)
and per worker/notification thread from the owning user's User.lang. Default: DEFAULT_LANG env (en).
"""
from __future__ import annotations

import contextvars
import json
import os

LANGS = ("en", "zh")
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "en") if os.environ.get("DEFAULT_LANG", "en") in LANGS else "en"
_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "i18n_en.json"), encoding="utf-8") as _f:
    EN: dict[str, str] = json.load(_f)

_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("ovhsn_lang", default=None)


def norm(lang: str | None) -> str:
    lang = (lang or "").lower()[:2]
    return lang if lang in LANGS else DEFAULT_LANG


def get_lang() -> str:
    return _lang.get() or DEFAULT_LANG


def set_lang(lang: str | None):
    return _lang.set(norm(lang))


def reset_lang(token) -> None:
    try:
        _lang.reset(token)
    except (ValueError, RuntimeError):
        pass


class LStr(str):
    """A translated string that remembers its message id + arguments, so it can be re-rendered in another
    language (task logs are written in the panel language, Telegram may use a different one)."""
    __slots__ = ("parts",)

    def __add__(self, other):
        return LCat([self, other])

    def __radd__(self, other):
        return LCat([other, self])


class LCat(LStr):
    def __new__(cls, parts):
        flat = []
        for p in parts:
            flat.extend(p.parts if isinstance(p, LCat) else [p])
        o = str.__new__(cls, "".join(str(p) for p in flat))
        o.parts = flat
        return o


class LMsg(LStr):
    def __new__(cls, text, msg, args, kw):
        o = str.__new__(cls, text)
        o.parts = (msg, args, kw)
        return o


def ljoin(sep: str, items) -> str:
    """'sep'.join() that keeps re-renderable parts."""
    out = []
    for i, x in enumerate(items):
        if i:
            out.append(sep)
        out.append(x)
    return LCat(out) if out else ""


def _render(msg, args, kw):
    s = EN.get(msg, msg) if get_lang() == "en" else msg
    if args or kw:
        try:
            return s.format(*args, **kw)
        except (IndexError, KeyError, ValueError):
            return msg.format(*args, **kw)
    return s


def T(msg: str, *args, **kw) -> str:
    """Translate (when English) then str.format() with the given arguments."""
    if msg is None:
        return ""
    return LMsg(_render(msg, args, kw), msg, args, kw)


def relang(s, lang: str | None):
    """Re-render a string produced by T()/+/ljoin in `lang`; plain strings are returned unchanged."""
    if not isinstance(s, LStr):
        return s
    with lang_scope(lang):
        return _re(s)


def _re(s):
    if isinstance(s, LCat):
        return "".join(_re(p) for p in s.parts)
    if isinstance(s, LMsg):
        msg, args, kw = s.parts
        return _render(msg, tuple(_re(a) for a in args), {k: _re(v) for k, v in kw.items()})
    return str(s) if isinstance(s, str) else s


def tr(msg: str | None) -> str:
    """Translate a value that may already be a final string (e.g. a constant table entry); unknown text passes through."""
    if not msg or not isinstance(msg, str):
        return msg or ""
    return LMsg(_render(msg, (), {}), msg, (), {})


class lang_scope:
    """with lang_scope(user.lang): ... — for worker threads, notifications and CLI."""

    def __init__(self, lang: str | None):
        self.lang = lang

    def __enter__(self):
        self.tok = set_lang(self.lang)
        return self

    def __exit__(self, *exc):
        reset_lang(self.tok)
        return False


def js_catalog() -> dict:
    """Subset of the catalog used by static JS (keys collected at build time into i18n_js_keys.json)."""
    try:
        with open(os.path.join(_HERE, "i18n_js_keys.json"), encoding="utf-8") as f:
            keys = json.load(f)
    except OSError:
        keys = list(EN)
    return {k: EN[k] for k in keys if k in EN}


def _tv(v):
    if isinstance(v, str):   # LMsg: rendered now, and re-renderable by relang() (e.g. Telegram in another language)
        return LMsg(_render(v, (), {}), v, (), {})
    if isinstance(v, tuple):
        return tuple(_tv(x) for x in v)
    if isinstance(v, list):
        return [_tv(x) for x in v]
    return v


class TDict(dict):
    """A constant lookup table whose (Chinese) values are translated on read, in the reader's language."""

    def __getitem__(self, k):
        return _tv(dict.__getitem__(self, k))

    def get(self, k, default=None):
        return _tv(dict.__getitem__(self, k)) if k in self else default

    def items(self):
        return [(k, _tv(v)) for k, v in dict.items(self)]

    def values(self):
        return [_tv(v) for v in dict.values(self)]
