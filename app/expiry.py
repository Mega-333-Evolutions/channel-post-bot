"""Timed deletion: a broadcast with an expiry ("/broadcast 1h") is removed from every channel - and from My posts -
when its time is up.

The deletions are saved in the database (table scheduled_deletes), so a restart loses nothing: posts that came due while
the bot was off are deleted as soon as it is back. The Expirer wakes up only when something is due; it does not poll the
database, so a sleeping serverless database stays asleep.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .db import as_utc, utcnow
from .repost_engine import delete_batch
from .tgutil import esc, peer_of

log = logging.getLogger(__name__)

MIN_SECONDS = 60
MAX_SECONDS = 366 * 86400
MAX_ATTEMPTS = 5
RETRY_AFTER = 600  # seconds between attempts, times the attempt number
LOCK_WAIT = 60  # a long job (repost...) is running: look again in a minute

_UNITS = {
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}  # fmt: skip
_PART = re.compile(r"(\d+)\s*([a-z]+)")
DURATION_HELP = "Use minutes, hours or days: 50m, 1h, 5d (or 1d12h). Leave the time out to keep the message forever."


def parse_duration(text: str) -> int:
    """'50m' -> 3000, '1h' -> 3600, '5d' -> 432000, '1d12h' -> 129600 (seconds). ValueError with a message if unusable."""
    t = (text or "").strip().lower().replace(",", " ")
    if not t or not re.fullmatch(r"(?:\d+\s*[a-z]+\s*)+", t):
        raise ValueError(f"I can't read “{(text or '').strip()[:30]}” as a time. {DURATION_HELP}")
    total = 0
    for n, unit in _PART.findall(t):
        if unit not in _UNITS:
            raise ValueError(f"“{n}{unit}” - I don't know the unit “{unit}”. {DURATION_HELP}")
        total += int(n) * _UNITS[unit]
    if total < MIN_SECONDS:
        raise ValueError("The shortest time is 1 minute.")
    if total > MAX_SECONDS:
        raise ValueError("The longest time is 1 year (365d).")
    return total


def human(seconds: int) -> str:
    """3000 -> '50 minutes', 3600 -> '1 hour', 129600 -> '1 day 12 hours'."""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    for n, word in ((days, "day"), (hours, "hour"), (minutes, "minute")):
        if n:
            parts.append(f"{n} {word}" + ("s" if n != 1 else ""))
    return " ".join(parts[:2]) or "under a minute"


def stamp(dt: datetime) -> str:
    return as_utc(dt).strftime("%d %b %Y, %H:%M UTC")


class Expirer:
    """Deletes scheduled messages when they come due. `start()` runs it in the background."""

    def __init__(self, ctx, *, notify=None):
        self.ctx = ctx
        self._next: Optional[datetime] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._notify = notify  # async fn(user_id, text) used to tell someone a deletion failed for good

    # ------------------------------------------------------------------------- running
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="expirer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def notify(self, when: datetime) -> None:
        """Something was scheduled for `when`: make sure the loop wakes up in time."""
        when = as_utc(when)
        if self._next is None or when < self._next:
            self._next = when
        self._wake.set()

    async def load(self) -> None:
        """The one look at the database (when the bot starts): when is the first deletion due?"""
        try:
            self._next = await self.ctx.db.next_delete_at()
        except Exception:
            log.exception("could not read the scheduled deletions")

    async def run(self) -> None:
        await self.load()
        while True:
            delay = None if self._next is None else max(0.0, (self._next - utcnow()).total_seconds())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._next is not None and self._next <= utcnow():
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("deleting scheduled messages failed")
                    self._next = utcnow() + timedelta(seconds=RETRY_AFTER)

    # --------------------------------------------------------------------------- work
    async def sweep(self, now: Optional[datetime] = None) -> dict:
        """Delete everything that is due at `now`. Returns {'deleted': n, 'retry': n, 'failed': n}."""
        ctx, db = self.ctx, self.ctx.db
        now = now or utcnow()
        stats = {"deleted": 0, "retry": 0, "failed": 0, "waiting": False}
        due = await db.due_deletes(now)
        if due and ctx.lock.locked():  # a repost / shift is running: don't pull posts out from under it
            stats["waiting"] = True
            self._next = now + timedelta(seconds=LOCK_WAIT)
            return stats
        by_channel: dict = {}
        for row in due:
            by_channel.setdefault(row.channel_id, []).append(row)
        for cid, rows in by_channel.items():
            await self._sweep_channel(cid, rows, now, stats)
        self._next = await db.next_delete_at()
        return stats

    async def _sweep_channel(self, cid: int, rows: list, now: datetime, stats: dict) -> None:
        ctx, db = self.ctx, self.ctx.db
        ch = await db.get_channel(cid)
        if ch is None:
            for r in rows:
                await db.fail_delete(r.id, "the channel was removed from the bot")
                stats["failed"] += 1
            return
        peer = peer_of(ch)
        ids = sorted({r.message_id for r in rows})
        gone: set = set()
        error: Optional[str] = None
        try:
            for i in range(0, len(ids), 100):
                out = await delete_batch(ctx.client, peer, ids[i : i + 100], None)
                gone |= out.gone
                error = error or out.bot_error
            left = [m for m in ids if m not in gone]
            ub = getattr(ctx, "userbot", None)
            if left and ub is not None and ub.enabled:  # the bot may not delete old posts: the userbot tries
                fallback, deleter = await ub.fallback_for(ch)
                try:
                    if fallback is not None:
                        for i in range(0, len(left), 100):
                            out = await delete_batch(ctx.client, peer, left[i : i + 100], fallback)
                            gone |= out.gone
                            error = out.fallback_error or error
                finally:
                    if deleter is not None:
                        await deleter.close()
        except Exception as e:
            log.exception("deleting scheduled messages of channel %s failed", cid)
            error = type(e).__name__
        done = [r for r in rows if r.message_id in gone]
        await db.finish_deletes([r.id for r in done])
        await db.forget_posts(cid, sorted(gone))
        stats["deleted"] += len(done)
        for r in rows:
            if r.message_id in gone:
                continue
            why = error or "Telegram did not delete it"
            if r.attempts + 1 >= MAX_ATTEMPTS:
                await db.fail_delete(r.id, why)
                stats["failed"] += 1
                await self._tell(r, ch, why)
            else:
                await db.retry_delete(r.id, at=now + timedelta(seconds=RETRY_AFTER * (r.attempts + 1)), error=why)
                stats["retry"] += 1

    async def _tell(self, row, ch, why: str) -> None:
        """A deletion failed for good: tell the person who sent the broadcast."""
        text = (
            f"⚠️ I could not delete the expired broadcast post {row.message_id} in <b>{esc(ch.title)}</b> "
            f"({esc(why)}). Delete it by hand, then use “Only forget it in the bot” in /posts."
        )
        try:
            if self._notify is not None:
                await self._notify(row.created_by, text)
            elif row.created_by:
                await self.ctx.client.send_message(row.created_by, text, link_preview=False)
        except Exception:
            log.warning("could not tell user %s about the failed deletion", row.created_by, exc_info=True)
