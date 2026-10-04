"""Makes the USERBOT_SESSION text. Run it on YOUR OWN computer, never on the server:

    pip install telethon
    python make_userbot_session.py

It asks for the phone number of the account, the login code Telegram sends to that account and, if the account has
one, its two-step password. Then it prints  USERBOT_SESSION=...  - put that line into the bot's secrets.

Use a separate Telegram account for this, not your main one: whoever has the session text can act as that account.
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(os.getenv("API_ID") or input("API_ID (from my.telegram.org): ").strip())
    api_hash = os.getenv("API_HASH") or input("API_HASH: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:  # prompts for phone, code, password
        me = await client.get_me()
        print(f"\nLogged in as {me.first_name} (id {me.id}).")
        print("\nUSERBOT_SESSION=" + client.session.save())
        print("\nKeep this text secret. Telegram > Settings > Devices shows the session; end it there to cancel it.")


asyncio.run(main())
