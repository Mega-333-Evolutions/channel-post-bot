"""bot.main(): start-up order, the command menu, the error log chat, the optional userbot, and what happens on a crash."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from telethon import functions, types

import bot
from app.config import Config
from app.errorlog import TelegramLogHandler

from .harness import FakeTG

CHAT = -1002525172451
TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ"


class StartupTG(FakeTG):
    """The bot's connection with the few extra methods main() uses."""

    def __init__(self, *, start_error=None, log_chat_known=True):
        super().__init__()
        self.start_error = start_error
        self.connected = True
        self.authorized = True
        self.calls = []
        self.menu = None
        if log_chat_known:
            self.known_users[CHAT] = SimpleNamespace(id=2525172451, access_hash=7)

    async def start(self, bot_token=None):
        self.calls.append(("start", bot_token))
        if self.start_error:
            raise self.start_error

    async def get_me(self):
        return SimpleNamespace(username="personalbuttonbot", id=42)

    async def run_until_disconnected(self):
        self.calls.append(("run", None))

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        self.calls.append(("connect", None))

    async def is_user_authorized(self):
        return self.authorized

    async def sign_in(self, bot_token=None):
        self.authorized = True
        self.calls.append(("sign_in", bot_token))

    async def __call__(self, req):
        if isinstance(req, functions.bots.SetBotCommandsRequest):
            self.menu = [(c.command, c.description) for c in req.commands]
            return True
        return await super().__call__(req)

    async def get_input_entity(self, peer):
        if isinstance(peer, int) and peer in self.known_users:
            return types.InputPeerChannel(2525172451, 7)
        return await super().get_input_entity(peer)


def make_cfg(tmp_path, **kw):
    base = dict(
        api_id=1, api_hash="hashhashhash", bot_token=TOKEN, owners=frozenset({1}), admins=frozenset(),
        database_url=f"sqlite+aiosqlite:///{tmp_path}/main.db", db_pool="null", session_path=str(tmp_path / "s"),
        session_string="", edit_delay=0.3, replace_typed_links=True, port=0, log_level="INFO", error_log_chat_id=CHAT,
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
def run_main(monkeypatch):
    """Runs bot.main() with the given config and connection; always removes the log handler main() installs."""

    def go(cfg, tg):
        monkeypatch.setattr(bot, "load_config", lambda: cfg)
        monkeypatch.setattr(bot, "build_client", lambda c: tg)
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            asyncio.run(bot.main())
        finally:
            for h in list(root.handlers):
                if h not in before and isinstance(h, TelegramLogHandler):
                    root.removeHandler(h)

    return go


def test_normal_start_up(tmp_path, run_main):
    tg = StartupTG()
    run_main(make_cfg(tmp_path), tg)
    assert tg.calls == [("start", TOKEN), ("run", None)]
    assert tg.menu == [(c, d) for c, d in bot.COMMANDS]
    names = [c for c, _ in tg.menu]
    assert "repost" in names and "userbot" in names and "testerror" in names
    assert "testedit" not in names and "selftest" not in names
    assert tg.handlers  # every module registered its handlers
    assert tg.sent == []  # nothing needed reporting


def test_a_log_chat_the_bot_is_not_in_does_not_stop_it(tmp_path, run_main, caplog):
    tg = StartupTG(log_chat_known=False)
    with caplog.at_level(logging.INFO):
        run_main(make_cfg(tmp_path), tg)
    assert ("run", None) in tg.calls
    assert any("errors cannot be sent to chat" in r.getMessage() and "add the bot to that chat" in r.getMessage() for r in caplog.records)


def test_log_chat_can_be_switched_off(tmp_path, run_main, caplog):
    tg = StartupTG(log_chat_known=False)
    with caplog.at_level(logging.INFO):
        run_main(make_cfg(tmp_path, error_log_chat_id=None), tg)
    assert ("run", None) in tg.calls and any("error log chat: off" in r.getMessage() for r in caplog.records)


def test_a_broken_userbot_session_does_not_stop_the_bot(tmp_path, run_main, caplog):
    tg = StartupTG()
    with caplog.at_level(logging.INFO):
        run_main(make_cfg(tmp_path, userbot_session="this is not a session"), tg)
    assert ("run", None) in tg.calls
    assert any("the userbot is not available" in r.getMessage() for r in caplog.records)


def test_the_health_endpoint_is_started_when_a_port_is_set(tmp_path, run_main, caplog):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tg = StartupTG()
    with caplog.at_level(logging.INFO):
        run_main(make_cfg(tmp_path, port=port), tg)
    assert any(f"health endpoint listening on :{port}" in r.getMessage() for r in caplog.records)


def test_a_crash_is_reported_to_the_log_chat_and_still_raised(tmp_path, run_main):
    tg = StartupTG(start_error=RuntimeError("login failed for 123456:ABCdefGHIjklMNOpqrSTUvwxYZ"))
    tg.connected = False  # the connection is gone when the crash happens
    tg.authorized = False
    with pytest.raises(RuntimeError, match="login failed"):
        run_main(make_cfg(tmp_path), tg)
    (kind, peer, text, kw), = tg.sent
    assert text.startswith("The bot stopped because of an unhandled exception:\n\nTraceback (most recent call last):")
    assert "RuntimeError: login failed for ***" in text and TOKEN not in text  # the token is never sent
    ent, = kw["formatting_entities"]
    assert isinstance(ent, types.MessageEntityPre) and ent.language == "python"
    assert ("connect", None) in tg.calls and ("sign_in", TOKEN) in tg.calls  # it reconnected just to say this


def test_a_crash_without_a_log_chat_is_just_raised(tmp_path, run_main):
    tg = StartupTG(start_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        run_main(make_cfg(tmp_path, error_log_chat_id=None), tg)
    assert tg.sent == []
