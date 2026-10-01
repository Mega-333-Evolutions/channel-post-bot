"""Settings, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv is optional; real env vars always win
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _ints(name: str) -> frozenset:
    raw = os.getenv(name, "") or ""
    out = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owners: frozenset
    admins: frozenset
    database_url: str
    db_pool: str
    session_path: str
    session_string: str
    edit_delay: float
    replace_typed_links: bool
    port: int
    log_level: str

    def is_owner(self, uid) -> bool:
        return uid in self.owners

    def is_allowed(self, uid) -> bool:
        return uid in self.owners or uid in self.admins


def load_config() -> Config:
    missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(k)]
    owners = _ints("OWNER_IDS")
    if not owners:
        missing.append("OWNER_IDS")
    if missing:
        raise SystemExit("Missing required settings: " + ", ".join(missing) + " (see .env.example)")
    try:
        api_id = int(os.environ["API_ID"])
    except ValueError:
        raise SystemExit("API_ID must be a number")
    return Config(
        api_id=api_id,
        api_hash=os.environ["API_HASH"].strip(),
        bot_token=os.environ["BOT_TOKEN"].strip(),
        owners=owners,
        admins=_ints("ADMIN_IDS"),
        database_url=(os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///data/bot.db").strip(),
        db_pool=(os.getenv("DB_POOL") or "null").strip().lower(),
        session_path=(os.getenv("SESSION_PATH") or "data/bot").strip(),
        session_string=(os.getenv("SESSION_STRING") or "").strip(),
        edit_delay=max(0.3, _float("EDIT_DELAY", 1.2)),
        replace_typed_links=_bool("REPLACE_TYPED_LINKS", True),
        port=int(os.getenv("PORT") or 0),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    )
