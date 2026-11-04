"""Settings, read from environment variables (or a .env file)."""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

try:  # python-dotenv is optional; real env vars always win
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env(name: str) -> str:
    """An environment variable by name, whatever the capital letters (DB_Channel, DB_CHANNEL and db_channel are one setting)."""
    raw = os.getenv(name)
    if raw is None:
        wanted = name.lower()
        raw = next((v for k, v in os.environ.items() if k.lower() == wanted), None)
    return (raw or "").strip()


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


DEFAULT_ERROR_LOG_CHAT_ID = -1002525172451
DEFAULT_AUTORENAME_BOT_ID = 8926792949
MIN_SHARE_URL_WAIT = 10.0  # the file-store bot needs a few seconds to put its button on a new post: never give up sooner
DEFAULT_SHARE_URL_WAIT = 45.0
_OFF = {"0", "off", "none", "false", "no", "disabled", "disable"}


def _chat_id(name: str, default: Optional[int]) -> Optional[int]:
    """A chat id from the environment: unset or empty -> default, "0" / "off" -> None (switched off)."""
    raw = (os.getenv(name) or "").strip()
    if raw == "":
        return default
    if raw.lower() in _OFF:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a chat id such as -1001234567890 (or 0 / off to switch it off), not {raw!r}")


def _chat_ref(name: str) -> Optional[int]:
    """A chat id such as -1001234567890 (or a bare channel id) from the environment; unset or empty -> None."""
    raw = _env(name)
    if raw == "" or raw.lower() in _OFF:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a numeric chat id such as -1001234567890, not {raw!r}")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _wait_seconds(name: str) -> float:
    """How long to wait for the Share URL button: at least MIN_SHARE_URL_WAIT, at most ten minutes."""
    value = _float(name, DEFAULT_SHARE_URL_WAIT)
    if not math.isfinite(value):
        value = DEFAULT_SHARE_URL_WAIT
    return min(600.0, max(MIN_SHARE_URL_WAIT, value))


def _whole(name: str, default: int) -> int:
    """A whole number from the environment; anything that is not a finite number gives the default."""
    value = _float(name, float(default))
    return int(value) if math.isfinite(value) else default


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
    error_log_chat_id: Optional[int] = None  # errors are sent to this chat; None = off
    userbot_session: str = ""  # session text of the helper account that deletes old posts
    userbot_keep_admin: bool = False  # keep the userbot an admin after a clean-up (default: take the right away)
    delete_service_messages: bool = True  # remove "channel name changed", "pinned a message", video chat ... notices
    sync_interval_minutes: int = 60  # how often every channel is compared with My posts (0 = never on its own)
    import_old_posts: bool = True  # read the posts that were in a channel before the bot was added into My posts
    markup_input: bool = True  # text typed as HTML or Markdown (and sent without Telegram formatting) becomes real formatting
    db_channel: Optional[int] = None  # the Database Channel the autorename bot copies renamed episodes into (DB_CHANNEL)
    comm_chat: Optional[int] = None  # the chat the autorename bot and this bot talk in (COMMUNICATION_CHAT)
    autorename_bot_id: int = DEFAULT_AUTORENAME_BOT_ID  # the only account whose messages in that chat are acted on
    share_url_wait: float = DEFAULT_SHARE_URL_WAIT  # seconds to wait for the 🔁 Share URL button of a new Database Channel post

    @property
    def auto_publish(self) -> bool:
        """Posts are published by themselves when the autorename bot reports a file: both chats have to be known."""
        return bool(self.db_channel and self.comm_chat)

    def is_owner(self, uid) -> bool:
        return uid in self.owners

    def is_allowed(self, uid) -> bool:
        return uid in self.owners or uid in self.admins


def default_session_path() -> str:
    """Outside the app folder on purpose: a host that publishes that folder (python -m http.server) must not
    publish the login session. A bot can always log in again with its token, so losing it is harmless."""
    return os.path.join(tempfile.gettempdir(), "channel-post-bot", "bot")


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
        admins=frozenset(),
        database_url=(os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///data/bot.db").strip(),
        db_pool=(os.getenv("DB_POOL") or "null").strip().lower(),
        session_path=(os.getenv("SESSION_PATH") or default_session_path()).strip(),
        session_string=(os.getenv("SESSION_STRING") or "").strip(),
        edit_delay=max(0.3, _float("EDIT_DELAY", 1.2)),
        replace_typed_links=_bool("REPLACE_TYPED_LINKS", True),
        port=int(os.getenv("PORT") or 0),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        error_log_chat_id=_chat_id("ERROR_LOG_CHAT_ID", DEFAULT_ERROR_LOG_CHAT_ID),
        userbot_session=(os.getenv("USERBOT_SESSION") or "").strip(),
        userbot_keep_admin=_bool("USERBOT_KEEP_ADMIN", False),
        delete_service_messages=_bool("DELETE_SERVICE_MESSAGES", True),
        sync_interval_minutes=max(0, _whole("SYNC_INTERVAL_MINUTES", 60)),
        import_old_posts=_bool("IMPORT_OLD_POSTS", True),
        markup_input=_bool("MARKUP_INPUT", True),
        db_channel=_chat_ref("DB_CHANNEL"),
        comm_chat=_chat_ref("COMMUNICATION_CHAT"),
        autorename_bot_id=_chat_ref("AUTORENAME_BOT_ID") or DEFAULT_AUTORENAME_BOT_ID,
        share_url_wait=_wait_seconds("SHARE_URL_WAIT_SECONDS"),
    )
