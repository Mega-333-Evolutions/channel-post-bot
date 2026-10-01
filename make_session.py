"""Prints a SESSION_STRING for hosts that lose local files on restart:  python make_session.py"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import load_config


async def main() -> None:
    cfg = load_config()
    async with TelegramClient(StringSession(), cfg.api_id, cfg.api_hash) as client:
        await client.start(bot_token=cfg.bot_token)
        print("\nSESSION_STRING=" + client.session.save())


asyncio.run(main())
