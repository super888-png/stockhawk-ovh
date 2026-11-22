"""Runtime configuration from environment variables."""
import os

SECRET_KEY = os.environ.get("SECRET_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}")
CACHE_DIR = os.path.join(DATA_DIR, "catalog_cache")
# Minimum allowed poll interval (seconds). Enforced server-side for every task.
MIN_POLL_INTERVAL = float(os.environ.get("MIN_POLL_INTERVAL", "5"))
# Set COOKIE_SECURE=1 when served over HTTPS (recommended in production).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
APP_TZ = os.environ.get("APP_TZ", "Asia/Shanghai")
# Set TRUST_PROXY=1 only behind a reverse proxy that sets X-Forwarded-Host (used by the same-origin check).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# Extra origins accepted by the same-origin check, e.g. "https://panel.example.com" (comma separated). Usually not needed:
# the Origin header is compared with the Host header, which Cloudflare/Caddy/Nginx pass through unchanged.
ALLOWED_ORIGINS = {o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}
CATALOG_TTL = int(os.environ.get("CATALOG_TTL", "3600"))
LOG_KEEP_PER_TASK = int(os.environ.get("LOG_KEEP_PER_TASK", "3000"))

if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise SystemExit("SECRET_KEY is missing or too short (at least 32 characters). Generate one: python -c \"import secrets;print(secrets.token_urlsafe(48))\"")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

