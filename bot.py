"""Channel Post Bot - entry point.  Run:  python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from app.common import Ctx
from app.config import Config, load_config
from app.db import Database
from app.handlers import basic, channels, create, posts, replace
from app.health import start_health_server

log = logging.getLogger("bot")

COMMANDS = [
    ("new", "Create a post"),
    ("posts", "My posts and drafts"),
    ("addchannel", "Register a channel"),
    ("channels", "List channels"),
    ("replace", "Swap a username in links (owner)"),
    ("undo", "Undo the last replace (owner)"),
    ("testedit", "Test editing one post (owner)"),
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
    for module in (basic, channels, create, posts, replace):
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


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)

    db = Database(cfg.database_url, cfg.db_pool)
    await db.init()
    client = build_client(cfg)
    ctx = Ctx(cfg=cfg, db=db, client=client)
    register_all(ctx)
    try:
        await client.start(bot_token=cfg.bot_token)
        me = await client.get_me()
        log.info("Logged in as @%s (owners: %s)", me.username, ", ".join(map(str, sorted(cfg.owners))))
        await set_commands(client)
        if cfg.port:
            await start_health_server(cfg.port)
        await client.run_until_disconnected()
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
