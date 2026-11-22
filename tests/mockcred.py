"""Credentials of the throwaway demo account created by tests/run_mock_server.py (random, stored in /tmp)."""
import json
import os
import secrets

PATH = os.environ.get("MOCK_CRED_FILE", "/tmp/ovh-sniper-mock-cred.json")


def ensure() -> tuple[str, str]:
    """Load, or create once, the demo login shared by every mock server port."""
    if os.path.exists(PATH):
        d = json.load(open(PATH))
        return d["user"], d["password"]
    d = {"user": "demo", "password": "Demo-" + secrets.token_urlsafe(12)}
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    return d["user"], d["password"]


def get() -> tuple[str, str]:
    if os.environ.get("MOCK_USER") and os.environ.get("MOCK_PASS"):
        return os.environ["MOCK_USER"], os.environ["MOCK_PASS"]
    return ensure()
