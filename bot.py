"""Channel Post Bot - entry point.  Run:  python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import unquote, urlsplit

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from app.common import Ctx
from app.config import Config, load_config
from app.db import Database
from app.errorlog import ErrorReporter, TelegramLogHandler
from app.handlers import basic, channels, checkbtn, create, posts, replace, repost
from app.health import start_health_server
from app.userbot import Userbot

log = logging.getLogger("bot")

COMMANDS = [
    ("new", "Create a post"),
    ("posts", "My posts and drafts"),
    ("addchannel", "Register a channel"),
    ("channels", "List channels"),
    ("replace", "Swap a username in links (owner)"),
    ("repost", "Copy a whole channel in order (owner)"),
    ("undo", "Undo the last replace (owner)"),
    ("userbot", "Status of the helper account that deletes old posts (owner)"),
    ("testerror", "Send a test error to the log chat (owner)"),
    ("export", "Backup as JSON (owner)"),
    ("cancel", "Cancel what I'm doing"),
    ("help", "Show help"),
]


def build_client(cfg: Config) -> TelegramClient:
    if cfg.session_string:
        session = StringSession(cfg.session_string)
    else:
        os.makedirs(os.path.dirname(cfg.session_path) or ".", exist_ok=True)
        session = cfg.session_path
    client = TelegramClient(session, cfg.api_id, cfg.api_hash)
    client.parse_mode = "html"
    return client


def register_all(ctx: Ctx) -> None:
    for module in (basic, channels, create, posts, checkbtn, replace, repost):
        module.register(ctx)


async def set_commands(client: TelegramClient) -> None:
    try:
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="",
                commands=[types.BotCommand(command=c, description=d) for c, d in COMMANDS],
            )
        )
    except Exception as e:  # cosmetic only
        log.warning("could not set the command menu: %s", e)


def secrets_of(cfg: Config) -> list:
    """Texts that must never appear in the error log."""
    out = [cfg.bot_token, cfg.api_hash, cfg.session_string, cfg.userbot_session]
    try:
        parts = urlsplit(cfg.database_url)
        if parts.password:
            out += [parts.password, unquote(parts.password)]
    except Exception:
        pass
    return [s for s in out if s]


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    client = build_client(cfg)
    reporter = ErrorReporter(client, cfg.error_log_chat_id, bot_token=cfg.bot_token, secrets=secrets_of(cfg))
    reporter.start()
    logging.getLogger().addHandler(TelegramLogHandler(reporter))
    asyncio.get_running_loop().set_exception_handler(reporter.loop_exception_handler)

    db = None
    userbot = Userbot(client, cfg)
    try:
        db = Database(cfg.database_url, cfg.db_pool)
        await db.init()
        ctx = Ctx(cfg=cfg, db=db, client=client, userbot=userbot, reporter=reporter)
        register_all(ctx)
        await client.start(bot_token=cfg.bot_token)
        me = await client.get_me()
        log.info("Logged in as @%s (owners: %s)", me.username, ", ".join(map(str, sorted(cfg.owners))))
        await set_commands(client)
        if cfg.port:
            await start_health_server(cfg.port)
        problem = await reporter.check()
        if reporter.enabled:
            if problem:
                log.warning("errors cannot be sent to chat %s yet: %s", cfg.error_log_chat_id, problem)
            else:
                log.info("errors are sent to chat %s", cfg.error_log_chat_id)
        else:
            log.info("error log chat: off")
        if userbot.enabled:
            await userbot.connect()  # never fatal: the userbot is only needed for old posts
        await client.run_until_disconnected()
    except Exception as e:
        await reporter.report_fatal("The bot stopped because of an unhandled exception", e)
        raise
    finally:
        await userbot.close()
        if db is not None:
            await db.close()
        await reporter.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
