"""Send errors to a Telegram chat (ERROR_LOG_CHAT_ID) as a `python` code block.

What is reported:
  * every log record of level ERROR or higher that carries a message / traceback (handler failures included),
  * exceptions nobody caught in background tasks ("Unhandled exception in background task <unnamed>:"),
  * a crash of the whole bot (sent right before it exits).
Long tracebacks are cut into several messages, never shortened. Secrets (bot token, API hash, session texts,
database password) are replaced by *** before anything leaves the bot. The reporter never raises, never blocks the
bot, and protects the chat from floods (identical reports are merged, at most a few per minute).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import traceback
from collections import deque
from typing import Optional

from telethon import types

from .tgutil import utf16_len

log = logging.getLogger(__name__)

PART_LIMIT = 3800  # characters per message (Telegram allows 4096; the rest is head-room for the part label)
PIECE = 1500  # a single line longer than a message is cut into pieces of this size
MUTE_SECONDS = 600  # after "can't write to the log chat" stay quiet this long
MIN_SECRET = 6  # shorter secrets are not redacted (they would mangle ordinary text)


# ----------------------------------------------------------------------------- text helpers
def split_report(text: str, limit: int = PART_LIMIT) -> list:
    """Cut a report into messages of at most `limit` characters at line breaks. Nothing is dropped."""
    if len(text) <= limit:
        return [text]
    room = limit - 40  # space for the "(part 3/12)" label
    pieces: list = []
    for line in text.split("\n"):
        while len(line) > room:  # a single huge line: cut it
            size = min(PIECE, room)
            pieces.append(line[:size])
            line = line[size:]
        pieces.append(line)
    parts: list = []
    cur: list = []  # the lines of the part being built
    used = 0
    for piece in pieces:
        add = len(piece) + (1 if cur else 0)
        if cur and used + add > room:
            parts.append("\n".join(cur))
            cur, used = [piece], len(piece)
        else:
            cur.append(piece)
            used += add
    if cur:
        parts.append("\n".join(cur))
    n = len(parts)
    return [f"(part {i}/{n})\n{p}" for i, p in enumerate(parts, 1)]


def redact(text: str, secrets) -> str:
    for s in sorted({s for s in secrets if s and len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _task_label(task) -> str:
    name = getattr(task, "get_name", lambda: "")() if task is not None else ""
    if not name or re.fullmatch(r"Task-\d+", name):
        return "<unnamed>"
    return name


# ---------------------------------------------------------------------------------- reporter
class ErrorReporter:
    def __init__(
        self,
        client,
        chat_id: Optional[int],
        *,
        bot_token: str = "",
        secrets=(),
        per_minute: int = 12,
        dedupe_seconds: float = 60.0,
        gap: float = 1.0,
        part_gap: float = 0.6,
    ):
        self.client = client
        self.chat_id = chat_id
        self.bot_token = bot_token
        self.secrets = [s for s in (*secrets, bot_token) if s]  # the login token is always hidden too
        self.per_minute = per_minute
        self.dedupe_seconds = dedupe_seconds
        self.gap = gap
        self.part_gap = part_gap
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._peer = None
        self._muted_until = 0.0
        self._recent: dict = {}  # report key -> [time of the first, number suppressed since]
        self._times: deque = deque()  # when reports were accepted (for the per-minute limit)
        self._skipped = 0  # reports dropped by the limit since the last one that went out

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id)

    # ------------------------------------------------------------------ life cycle
    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=100)
        self._task = self._loop.create_task(self._worker(), name="error-reporter")

    async def stop(self, wait: float = 5.0) -> None:
        """Let queued reports go out (for a few seconds at most), then stop."""
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), wait)
        except Exception:
            pass
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    # ---------------------------------------------------------------------- input
    def submit(self, title: str, tb: str = "") -> None:
        """Queue a report. Safe to call from any thread; never raises."""
        try:
            loop = self._loop
            if not self.enabled or loop is None or loop.is_closed():
                return
            try:
                here = asyncio.get_running_loop()
            except RuntimeError:
                here = None
            if here is loop:
                self._enqueue(title, tb)
            else:
                loop.call_soon_threadsafe(self._enqueue, title, tb)
        except Exception:
            pass

    def _enqueue(self, title: str, tb: str) -> None:
        try:
            now = time.monotonic()
            if now < self._muted_until or self._queue is None:
                return
            last_line = next((ln for ln in reversed(tb.splitlines()) if ln.strip()), "")
            key = hashlib.sha1(f"{title}|{last_line}".encode("utf-8", "replace")).hexdigest()
            prev = self._recent.get(key)
            merged = 0
            if prev is not None:
                if now - prev[0] < self.dedupe_seconds:
                    prev[1] += 1  # same error again: counted, reported with the next one
                    return
                merged = prev[1]
            self._recent[key] = [now, 0]
            if len(self._recent) > 200:
                for k in sorted(self._recent, key=lambda k: self._recent[k][0])[:100]:
                    self._recent.pop(k, None)
            while self._times and now - self._times[0] > 60:
                self._times.popleft()
            if len(self._times) >= self.per_minute:
                self._skipped += 1
                return
            self._times.append(now)
            skipped, self._skipped = self._skipped, 0
            try:
                self._queue.put_nowait((title, tb, merged, skipped))
            except asyncio.QueueFull:
                self._skipped += 1 + skipped
        except Exception:
            pass

    def loop_exception_handler(self, loop, context) -> None:
        """asyncio calls this for exceptions nobody retrieved (set it with loop.set_exception_handler)."""
        try:
            exc = context.get("exception")
            task = context.get("task") or context.get("future")
            if task is not None and exc is not None:
                title = f"Unhandled exception in background task {_task_label(task)}:"
            else:
                title = (context.get("message") or "Unhandled error in the event loop") + ":"
            tb = format_exception(exc) if exc is not None else ""
            self.submit(title, tb)
        except Exception:
            pass
        finally:
            loop.default_exception_handler(context)  # keep the normal log line too

    # --------------------------------------------------------------------- output
    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            try:
                await self._deliver(*item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                name = type(e).__name__
                log.warning("could not write to the error log chat: %s %s", name, str(e)[:150])
                if isinstance(e, (ValueError, PermissionError)) or "Forbidden" in name or "Private" in name:
                    # the bot is not a member of that chat (or may not write there): stop trying for a while
                    self._muted_until = time.monotonic() + MUTE_SECONDS
                    self._peer = None
            finally:
                self._queue.task_done()
            await asyncio.sleep(self.gap)

    async def _deliver(self, title: str, tb: str, merged: int = 0, skipped: int = 0) -> None:
        if self._peer is None:
            self._peer = await self.client.get_input_entity(self.chat_id)
        text = title + (("\n\n" + tb.rstrip("\n")) if tb else "")
        notes = []
        if merged:
            notes.append(f"(+{merged} identical report(s) suppressed)")
        if skipped:
            notes.append(f"({skipped} report(s) were skipped because too many errors came in)")
        if notes:
            text += "\n\n" + "\n".join(notes)
        text = redact(text, self.secrets)
        parts = split_report(text)
        for i, part in enumerate(parts):
            await self.client.send_message(
                self._peer,
                part,
                formatting_entities=[types.MessageEntityPre(0, utf16_len(part), "python")],
                link_preview=False,
            )
            if i < len(parts) - 1:
                await asyncio.sleep(self.part_gap)

    async def check(self) -> Optional[str]:
        """None when the log chat can be reached, otherwise a short reason (used once at start-up)."""
        if not self.enabled:
            return None
        try:
            self._peer = await self.client.get_input_entity(self.chat_id)
            return None
        except Exception as e:
            return (
                f"{type(e).__name__}: {str(e)[:120]} - add the bot to that chat "
                "(as a member, or as an admin who may post)"
            )

    async def report_fatal(self, title: str, exc: BaseException) -> None:
        """The bot is going down: send the crash straight away (the queue may not get the time)."""
        if not self.enabled:
            return
        try:

            async def go() -> None:
                if not self.client.is_connected():
                    await self.client.connect()
                if self.bot_token and not await self.client.is_user_authorized():
                    await self.client.sign_in(bot_token=self.bot_token)
                await self._deliver(title + ":", format_exception(exc))

            await asyncio.wait_for(go(), 20)
        except Exception as e:
            log.warning("could not send the crash report: %s %s", type(e).__name__, str(e)[:150])


# ------------------------------------------------------------------------------ logging
class TelegramLogHandler(logging.Handler):
    """Forwards log records (ERROR and up) to the reporter."""

    def __init__(self, reporter: ErrorReporter, level: int = logging.ERROR):
        super().__init__(level)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("app.errorlog") or record.name == "asyncio":
                return  # our own messages would loop; asyncio's go through loop_exception_handler
            title = record.getMessage()
            tb = ""
            if record.exc_info and record.exc_info[0] is not None:
                tb = "".join(traceback.format_exception(*record.exc_info))
                title += ":"
            if not record.name.startswith(("app", "bot", "__main__")):
                title = f"[{record.name}] {title}"
            self.reporter.submit(title, tb)
        except Exception:
            pass
