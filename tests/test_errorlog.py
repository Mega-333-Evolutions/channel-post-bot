"""Errors go to the log chat as a `python` code block: full tracebacks, split not shortened, secrets removed, no floods."""
import asyncio
import gc
import logging
import threading
from types import SimpleNamespace

import pytest
from telethon import errors, types

from app import errorlog
from app.errorlog import ErrorReporter, TelegramLogHandler, format_exception, redact, split_report
from app.tgutil import utf16_len

from .harness import App, norm

CHAT = -1002525172451
TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ"


class FakeSender:
    """The bot's Telegram connection, as far as the reporter uses it."""

    def __init__(self, *, member=True, connected=True, authorized=True):
        self.member, self.connected, self.authorized = member, connected, authorized
        self.sent = []  # (peer, text, entities, link_preview)
        self.lookups = []
        self.signed_in = []
        self.fail_send = None

    async def get_input_entity(self, chat_id):
        self.lookups.append(chat_id)
        if not self.member:
            raise ValueError(f"Could not find the input entity for {chat_id}")
        return types.InputPeerChannel(2525172451, 1)

    async def send_message(self, peer, text, *, formatting_entities=None, link_preview=None):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append((peer, text, formatting_entities, link_preview))

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def sign_in(self, bot_token=None):
        self.signed_in.append(bot_token)
        self.authorized = True

    @property
    def texts(self):
        return [s[1] for s in self.sent]


def reporter(client, **kw):
    kw.setdefault("gap", 0)
    kw.setdefault("part_gap", 0)
    kw.setdefault("bot_token", TOKEN)
    return ErrorReporter(client, kw.pop("chat_id", CHAT), **kw)


async def drain(rep, timeout=5):
    await asyncio.wait_for(rep._queue.join(), timeout)


def boom(depth=0):
    """An exception chain like the one in the screenshot: a cause, then the error it led to."""
    try:
        try:
            raise KeyError("inner")
        except KeyError as e:
            raise RuntimeError("outer") from e
    except RuntimeError as e:
        return e


# ------------------------------------------------------------------------------------ text helpers
def test_short_reports_stay_in_one_piece():
    assert split_report("hello") == ["hello"]
    text = "x" * 3800
    assert split_report(text) == [text]


def test_long_reports_are_split_at_line_breaks_and_nothing_is_lost():
    lines = [f"  File \"/app/module_{i}.py\", line {i}, in function_{i}" for i in range(300)]
    text = "Unhandled exception:\n" + "\n".join(lines)
    parts = split_report(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    n = len(parts)
    assert [p.split("\n", 1)[0] for p in parts] == [f"(part {i}/{n})" for i in range(1, n + 1)]
    body = "\n".join(p.split("\n", 1)[1] for p in parts)
    assert body == text  # the same lines in the same order, none dropped, none cut in half


def test_one_enormous_line_is_cut_into_pieces_not_dropped():
    text = "Error: " + "y" * 10000
    parts = split_report(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    body = "".join(p.split("\n", 1)[1] for p in parts)
    assert body.replace("\n", "") == text  # every character is there; the only addition is a line break at each cut


def test_redact_removes_secrets_longest_first():
    out = redact(f"token {TOKEN} and hunter2hunter2 and ab", [TOKEN, "hunter2hunter2", "ab", "", None])
    assert out == "token *** and *** and ab"  # a 2-letter "secret" would mangle ordinary words, so it is left alone
    assert redact("keep me", [TOKEN]) == "keep me"
    assert redact("p@ssw0rd-long p@ssw0rd", ["p@ssw0rd", "p@ssw0rd-long"]) == "*** ***"


def test_traceback_text_contains_the_whole_chain():
    text = format_exception(boom())
    assert text.startswith("Traceback (most recent call last):")
    assert "KeyError: 'inner'" in text and "RuntimeError: outer" in text
    assert "The above exception was the direct cause of the following exception:" in text


# ---------------------------------------------------------------------------------------- delivery
def test_a_report_is_sent_as_a_python_code_block():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        rep.submit("Unhandled exception in background task <unnamed>:", format_exception(boom()))
        await drain(rep)
        (peer, text, ents, preview), = client.sent
        assert text.startswith("Unhandled exception in background task <unnamed>:\n\nTraceback (most recent call last):")
        assert "The above exception was the direct cause of the following exception:" in text
        assert text.rstrip().endswith("RuntimeError: outer")
        # one code block over the whole message, language "python"
        (ent,) = ents
        assert isinstance(ent, types.MessageEntityPre) and ent.language == "python"
        assert (ent.offset, ent.length) == (0, utf16_len(text)) and preview is False
        assert client.lookups == [CHAT]
        await rep.stop()

    asyncio.run(go())


def test_the_code_block_length_counts_utf16_units():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        rep.submit("Fehler 😀 in Aufgabe:", "Traceback:\n  ValueError: Größe ≠ 😀😀")
        await drain(rep)
        _, text, ents, _ = client.sent[0]
        assert ents[0].length == utf16_len(text) and utf16_len(text) > len(text)  # emoji take two units
        await rep.stop()

    asyncio.run(go())


def test_a_long_traceback_is_sent_in_several_messages_and_every_line_arrives():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        tb = "Traceback (most recent call last):\n" + "\n".join(f'  File "/app/m{i}.py", line {i}, in f{i}' for i in range(400)) + "\nValueError: deep"
        rep.submit("Unhandled exception in handler h_x (user 1):", tb)
        await drain(rep)
        assert len(client.sent) > 1
        for _, text, ents, _ in client.sent:
            assert len(text) <= 4096 and ents[0].length == utf16_len(text) and ents[0].language == "python"
        joined = "\n".join(t.split("\n", 1)[1] for t in client.texts)
        assert "ValueError: deep" in joined and joined.count("File \"/app/m") == 400
        assert client.texts[0].startswith(f"(part 1/{len(client.sent)})")
        await rep.stop()

    asyncio.run(go())


def test_secrets_never_leave_the_bot():
    async def go():
        client = FakeSender()
        # the bot token is hidden even though it is only given as the login token, not in the list of secrets
        rep = reporter(client, secrets=["hunter2hunter2", "SESSIONTEXT123456"])
        rep.start()
        rep.submit(f"login with {TOKEN} failed:", "OperationalError: password hunter2hunter2 wrong, session SESSIONTEXT123456")
        await drain(rep)
        text = client.texts[0]
        assert TOKEN not in text and "hunter2hunter2" not in text and "SESSIONTEXT123456" not in text
        assert text.count("***") == 3
        await rep.stop()

    asyncio.run(go())


def test_secrets_are_removed_even_when_a_text_is_split_across_parts():
    async def go():
        client = FakeSender()
        rep = reporter(client, secrets=["hunter2hunter2"])
        rep.start()
        lines = ["line %d hunter2hunter2" % i for i in range(500)]
        rep.submit("Many lines:", "\n".join(lines))
        await drain(rep)
        assert len(client.sent) > 1 and not any("hunter2" in t for t in client.texts)
        await rep.stop()

    asyncio.run(go())


# --------------------------------------------------------------------------------------- the sources
def test_a_forgotten_background_task_is_reported_like_in_the_screenshot():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(rep.loop_exception_handler)

        async def boom_task():
            raise boom()

        t = loop.create_task(boom_task())
        await asyncio.sleep(0.05)  # it ran and failed; nobody looked at the result
        del t
        gc.collect()
        await asyncio.sleep(0.05)
        await drain(rep)
        text = client.texts[0]
        assert text.startswith("Unhandled exception in background task <unnamed>:\n\nTraceback")
        assert "RuntimeError: outer" in text and "The above exception was the direct cause" in text
        await rep.stop()

    asyncio.run(go())


def test_named_tasks_keep_their_name_and_other_loop_errors_their_message():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        loop = asyncio.get_running_loop()

        async def noop():
            raise ValueError("x")

        task = loop.create_task(noop(), name="cleanup-job")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        calls = []
        loop.default_exception_handler = lambda ctx: calls.append(ctx)  # the normal log line still happens
        rep.loop_exception_handler(loop, {"message": "Task exception was never retrieved", "exception": task.exception(), "future": task})
        rep.loop_exception_handler(loop, {"message": "Fatal error on transport", "exception": OSError("pipe closed")})
        rep.loop_exception_handler(loop, {"message": "Something odd without an exception"})
        await drain(rep)
        assert client.texts[0].startswith("Unhandled exception in background task cleanup-job:")
        assert client.texts[1] == "Fatal error on transport:\n\nOSError: pipe closed"
        assert client.texts[2].startswith("Something odd without an exception:")
        assert len(calls) == 3
        await rep.stop()

    asyncio.run(go())


def test_the_logging_handler_forwards_errors_with_their_traceback():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        handler = TelegramLogHandler(rep)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            try:
                raise boom()
            except RuntimeError:
                logging.getLogger("app.common").exception("Unhandled exception in handler h_posts (user 1)")
            logging.getLogger("app.common").error("Telegram refused a request")  # no traceback
            logging.getLogger("app.common").warning("only a warning")  # below ERROR: not sent
            logging.getLogger("telethon.network").error("Connection lost")  # other libraries get a tag
            logging.getLogger("app.errorlog").error("our own message must not loop")
            logging.getLogger("asyncio").error("asyncio has its own path")
            await asyncio.sleep(0.05)
            await drain(rep)
        finally:
            root.removeHandler(handler)
        assert len(client.sent) == 3, client.texts
        assert client.texts[0].startswith("Unhandled exception in handler h_posts (user 1):\n\nTraceback")
        assert "The above exception was the direct cause of the following exception:" in client.texts[0]
        assert client.texts[1] == "Telegram refused a request"
        assert client.texts[2] == "[telethon.network] Connection lost"
        await rep.stop()

    asyncio.run(go())


def test_a_failing_handler_in_the_bot_reaches_the_log_chat_end_to_end(tmp_path):
    """/testerror raises on purpose inside a real handler; guard() logs it; the log handler sends it."""

    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        handler = TelegramLogHandler(rep)
        logging.getLogger().addHandler(handler)
        app = App(tmp_path)
        await app.start()
        app.ctx.reporter = rep
        try:
            await app.text("/testerror")
            await asyncio.sleep(0.05)
            await drain(rep)
        finally:
            logging.getLogger().removeHandler(handler)
        assert "Raising a test error now" in norm(app.out.log[0][1])
        assert "Something went wrong: RuntimeError" in norm(app.out.last_text)  # the owner is told too
        text = client.texts[0]
        assert text.startswith("Unhandled exception in handler h_testerror (user 1):\n\nTraceback")
        assert "KeyError: 'test'" in text and "RuntimeError: Test error from /testerror" in text
        assert "The above exception was the direct cause of the following exception:" in text
        await app.db.close()
        await rep.stop()

    asyncio.run(go())


def test_testerror_task_makes_a_background_task_fail(tmp_path):
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        asyncio.get_running_loop().set_exception_handler(rep.loop_exception_handler)
        app = App(tmp_path)
        await app.start()
        app.ctx.reporter = rep
        await app.text("/testerror task")
        assert "background task will fail" in norm(app.out.last_text)
        await asyncio.sleep(0.1)
        gc.collect()
        await asyncio.sleep(0.05)
        await drain(rep)
        assert client.texts[0].startswith("Unhandled exception in background task <unnamed>:")
        assert "RuntimeError: Test error from /testerror task" in client.texts[0]
        await app.db.close()
        await rep.stop()

    asyncio.run(go())


def test_testerror_is_for_the_owner_and_says_when_the_log_is_off(tmp_path):
    async def go():
        app = App(tmp_path, admins=frozenset({7}))
        await app.start()
        await app.text("/testerror")  # no reporter at all
        assert "switched off" in norm(app.out.last_text)
        app.ctx.reporter = reporter(FakeSender(), chat_id=None)
        await app.text("/testerror")
        assert "switched off" in norm(app.out.last_text)
        app.uid = 7
        await app.text("/testerror")
        assert "private" in norm(app.out.last_text)
        await app.db.close()

    asyncio.run(go())


def test_a_crash_of_the_whole_bot_is_sent_directly_even_when_disconnected():
    async def go():
        client = FakeSender(connected=False, authorized=False)
        rep = reporter(client)  # never started: the crash path does not depend on the worker
        await rep.report_fatal("The bot stopped because of an unhandled exception", boom())
        assert client.connected and client.signed_in == [TOKEN]
        (peer, text, ents, _), = client.sent
        assert text.startswith("The bot stopped because of an unhandled exception:\n\nTraceback")
        assert "RuntimeError: outer" in text and ents[0].language == "python"

    asyncio.run(go())


def test_a_failing_crash_report_never_raises():
    async def go():
        client = FakeSender()
        client.fail_send = errors.ChatWriteForbiddenError(request=None)
        rep = reporter(client)
        await rep.report_fatal("The bot stopped", boom())  # must not raise (the bot is going down anyway)
        await reporter(FakeSender(), chat_id=None).report_fatal("x", boom())

    asyncio.run(go())


# ------------------------------------------------------------------------------- flood protection
def test_identical_errors_are_merged_and_counted(monkeypatch):
    async def go():
        now = [1000.0]
        monkeypatch.setattr(errorlog, "time", SimpleNamespace(monotonic=lambda: now[0]))
        client = FakeSender()
        rep = reporter(client, dedupe_seconds=60)
        rep.start()
        for _ in range(5):
            rep.submit("Unhandled exception in handler h:", "Traceback:\nValueError: same")
            now[0] += 1
        rep.submit("Unhandled exception in handler h:", "Traceback:\nValueError: different")  # another error: not merged
        await drain(rep)
        assert len(client.sent) == 2
        now[0] += 100  # a minute and a half later the same error is news again, and says how often it came meanwhile
        rep.submit("Unhandled exception in handler h:", "Traceback:\nValueError: same")
        await drain(rep)
        assert len(client.sent) == 3 and "(+4 identical report(s) suppressed)" in client.texts[2]
        assert "suppressed" not in client.texts[0]
        await rep.stop()

    asyncio.run(go())


def test_a_storm_of_different_errors_is_limited_per_minute(monkeypatch):
    async def go():
        now = [5000.0]
        monkeypatch.setattr(errorlog, "time", SimpleNamespace(monotonic=lambda: now[0]))
        client = FakeSender()
        rep = reporter(client, per_minute=3)
        rep.start()
        for i in range(10):
            rep.submit(f"error number {i}:", f"ValueError: {i}")
        await drain(rep)
        assert len(client.sent) == 3  # the first three, the other seven are counted
        now[0] += 61
        rep.submit("error after the storm:", "ValueError: later")
        await drain(rep)
        assert len(client.sent) == 4 and "(7 report(s) were skipped because too many errors came in)" in client.texts[3]
        await rep.stop()

    asyncio.run(go())


def test_not_being_allowed_to_write_to_the_chat_silences_the_reporter(monkeypatch):
    async def go():
        now = [10.0]
        monkeypatch.setattr(errorlog, "time", SimpleNamespace(monotonic=lambda: now[0]))
        client = FakeSender()
        client.fail_send = errors.ChatWriteForbiddenError(request=None)
        rep = reporter(client)
        rep.start()
        rep.submit("first:", "ValueError: 1")
        await drain(rep)
        assert client.sent == [] and client.lookups == [CHAT]
        rep.submit("second:", "ValueError: 2")  # muted: not even tried
        await asyncio.sleep(0.05)
        assert client.lookups == [CHAT] and rep._queue.qsize() == 0
        now[0] += errorlog.MUTE_SECONDS + 1  # after the pause it tries again (the owner may have added the bot meanwhile)
        client.fail_send = None
        rep.submit("third:", "ValueError: 3")
        await drain(rep)
        assert client.texts == ["third:\n\nValueError: 3"]
        await rep.stop()

    asyncio.run(go())


def test_other_sending_problems_do_not_silence_it():
    async def go():
        client = FakeSender()
        client.fail_send = RuntimeError("temporary network trouble")
        rep = reporter(client)
        rep.start()
        rep.submit("first:", "ValueError: 1")
        await drain(rep)
        client.fail_send = None
        rep.submit("second:", "ValueError: 2")
        await drain(rep)
        assert client.texts == ["second:\n\nValueError: 2"]
        await rep.stop()

    asyncio.run(go())


def test_a_chat_the_bot_is_not_in_is_noticed_at_start_up():
    async def go():
        rep = reporter(FakeSender(member=False))
        problem = await rep.check()
        assert "ValueError" in problem and "add the bot to that chat" in problem
        good = reporter(FakeSender())
        assert await good.check() is None
        assert await reporter(FakeSender(), chat_id=None).check() is None

    asyncio.run(go())


def test_switched_off():
    async def go():
        client = FakeSender()
        rep = reporter(client, chat_id=None)
        assert not rep.enabled
        rep.start()
        rep.submit("x:", "y")
        await rep.stop()
        assert client.sent == [] and client.lookups == [] and rep._task is None

    asyncio.run(go())


def test_submitting_from_another_thread_is_safe():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        t = threading.Thread(target=lambda: rep.submit("from a thread:", "ValueError: threaded"))
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        await drain(rep)
        assert client.texts == ["from a thread:\n\nValueError: threaded"]
        # and before the reporter runs, or after it has stopped, nothing breaks
        await rep.stop()
        rep.submit("late:", "x")
        reporter(FakeSender()).submit("never started:", "x")

    asyncio.run(go())


def test_stop_lets_queued_reports_go_out():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        rep.submit("last words:", "ValueError: bye")
        await rep.stop()
        assert client.texts == ["last words:\n\nValueError: bye"]

    asyncio.run(go())


def test_the_peer_is_looked_up_once():
    async def go():
        client = FakeSender()
        rep = reporter(client)
        rep.start()
        for i in range(3):
            rep.submit(f"e{i}:", f"ValueError: {i}")
        await drain(rep)
        assert len(client.sent) == 3 and client.lookups == [CHAT]
        await rep.stop()

    asyncio.run(go())


@pytest.mark.parametrize("raw,label", [("Task-12", "<unnamed>"), ("", "<unnamed>"), ("sender", "sender")])
def test_task_labels(raw, label):
    task = SimpleNamespace(get_name=lambda: raw)
    assert errorlog._task_label(task) == label
    assert errorlog._task_label(None) == "<unnamed>"
