import base64
import hashlib
import hmac
import re
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY
from .i18n import T, tr

# Fernet key derived from SECRET_KEY (separate derivation from the session-signing use).
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("fernet:" + SECRET_KEY).encode()).digest()))
SESSION_SECRET = hashlib.sha256(("session:" + SECRET_KEY).encode()).hexdigest()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except Exception:
        return False


def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode() if s else ""


def dec(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet.decrypt(s.encode()).decode()
    except InvalidToken:
        return ""


def mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return s[:4] + "*" * 8 + s[-4:]


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(session_token: str | None, given: str | None) -> bool:
    return bool(session_token and given and hmac.compare_digest(session_token, given))


USERNAME_RE = r"[A-Za-z0-9_.\-]{3,32}"

PW_COMMON = {"password", "passw0rd", "p@ssw0rd", "qwerty", "qwertyuiop", "asdf", "asdfgh", "zxcvbn", "1qaz2wsx", "iloveyou",
             "admin", "administrator", "root", "welcome", "letmein", "monkey", "dragon", "abc", "abcd", "abcdef",
             "ovh", "ovhcloud", "server", "changeme", "secret", "test", "guest", "user", "login", "woaini", "aaaaaa"}


def password_problem(pw: str, username: str) -> str | None:
    if len(pw) < 10:
        return T("密码至少 10 位")
    if len(pw) > 128:
        return T("密码最长 128 位")
    kinds = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if kinds < 2:
        return T("密码需包含字母、数字、符号中的至少两类")
    low = pw.lower()
    core = re.sub(r"^[^a-z]+|[^a-z]+$", "", low)          # "Password123!" -> "password"
    if low in PW_COMMON or core in PW_COMMON or len(set(low)) <= 3 or (username and username.lower() in low):
        return T("密码太常见、过于简单或包含用户名")
    return None
