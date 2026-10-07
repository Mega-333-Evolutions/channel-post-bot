"""The background side of the sync.

* a timer: every SYNC_INTERVAL_MINUTES each connected channel is compared with My posts (app/sync_engine.py);
* a queue for what Telegram tells the bot while it runs: a post deleted / edited / added in a channel is looked at a few
  seconds later (the wait lets a burst of changes settle, and lets the bot finish saving its own posts first);
* service messages ("pinned a message", "channel photo changed" ...) are deleted the moment they appear.

Nothing here holds the bot's one-long-job lock: a running /repost, /shift, /replace ... always wins, and what happens
while it runs is picked up by the next timed check.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .servicemsg import RIGHTS_ERRORS, action_label, remove
from .sync_engine import SyncOptions, SyncReport, sync_channel, sync_ids
from .tgutil import esc, peer_of

log = logging.getLogger(__name__)


class Syncer:
    def __init__(self, ctx):
        self.ctx = ctx
        # timings (seconds) - the tests make them small
        self.start_delay = 90.0  # after the bot starts, before the first timed check
        self.debounce = 5.0  # quiet time needed before the queue is worked off
        self.max_debounce = 30.0  # ... but never wait longer than this
        self.lock_wait = 30.0  # a long job runs: look again after this
        self.rt_grace = 10.0  # live changes: posts the bot touched / messages younger than this are judged next round
        self.pause = 0.25  # between two requests
        self.cache_ttl = 60.0  # how long "is this channel connected?" is remembered
        self.tell_every = 86400.0  # the same warning goes to the owners at most this often
        self.max_ids = 300  # more changed ids than this in one channel: look at the whole channel instead
        self.max_tries = 8  # a message that stays "too fresh" is given up after this many rounds
        self._busy = asyncio.Lock()  # one look at a time
        self._edits: dict = {}  # channel id -> message ids edited
        self._news: dict = {}  # channel id -> message ids that are new posts
        self._tries: dict = {}
        self._stamp = 0
        self._wake = asyncio.Event()
        self._tasks: list = []
        self._cache: dict = {}
        self._told: dict = {}
        self.last_reports: dict = {}  # channel id -> its latest SyncReport

    # ----------------------------------------------------------------------------------------- running
    @property
    def enabled(self) -> bool:
        return self.ctx.cfg.sync_interval_minutes > 0

    def start(self) -> None:
        if self._tasks or not self.enabled:
            return
        self._tasks = [
            asyncio.create_task(self._periodic(), name="sync-timer"),
            asyncio.create_task(self._events(), name="sync-live"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _periodic(self) -> None:
        await asyncio.sleep(self.start_delay)
        while True:
            try:
                await self.run_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the timed check of the channels failed")
            await asyncio.sleep(max(5, self.ctx.cfg.sync_interval_minutes) * 60)

    async def _events(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.flush_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("working off the channel changes failed")

    # ----------------------------------------------------------------------------------------- helpers
    async def channel(self, cid: int):
        """The connected channel with this id, or None (remembered for a minute: busy channels cost no database reads)."""
        now = time.monotonic()
        hit = self._cache.get(cid)
        if hit is not None and now - hit[0] < self.cache_ttl:
            return hit[1]
        ch = await self.ctx.db.get_channel(cid)
        if ch is not None and not ch.active:
            ch = None
        self._cache[cid] = (now, ch)
        return ch

    def forget_cache(self) -> None:
        self._cache.clear()

    def options(self, **kw) -> SyncOptions:
        ctx = self.ctx
        base = dict(delete_services=ctx.cfg.delete_service_messages, ignore=ctx.sync_ignore, pause=self.pause)
        base.update(kw)
        return SyncOptions(**base)

    async def tell_owners(self, ch, key: str, text: str) -> None:
        """A warning for the owners - the same one (per channel and kind) at most once a day."""
        now = time.monotonic()
        k = (ch.id, key)
        if now - self._told.get(k, -1e12) < self.tell_every:
            return
        self._told[k] = now
        for uid in sorted(self.ctx.cfg.owners):
            try:
                await self.ctx.client.send_message(uid, text, link_preview=False)
            except Exception:
                log.warning("could not warn user %s about %s", uid, ch.id, exc_info=True)

    async def _after(self, ch, rep: SyncReport) -> None:
        self.last_reports[ch.id] = rep
        if rep.changed or rep.error or rep.anomaly:
            log.info(
                "channel %s: %s removed, %s updated, %s new, %s service messages deleted%s",
                ch.id, len(rep.deleted), len(rep.edited), len(rep.adopted), rep.services_deleted,
                f" - {rep.error or rep.anomaly}" if (rep.error or rep.anomaly) else "",
            )
        if rep.services_failed and rep.services_error in RIGHTS_ERRORS:
            await self.tell_owners(ch, "rights", self._rights_text(ch, rep.services_error))

    @staticmethod
    def _rights_text(ch, why: Optional[str]) -> str:
        return (
            f"⚠️ I can't delete the service messages (name changes, “pinned a message” ...) in <b>{esc(ch.title)}</b> "
            f"({esc(why or 'refused')}). Give the bot the <i>Delete messages</i> right there, or set "
            "DELETE_SERVICE_MESSAGES=false to stop trying."
        )

    # ------------------------------------------------------------------------------------ full looks
    async def run_all(self, chans: Optional[list] = None, *, explicit: bool = False, clean: bool = False, progress=None) -> list:
        """Look at the connected channels one after the other. `explicit` (the /sync command): the caller holds the
        one-long-job lock and everything is read; the timed check gives way to any long job instead."""
        ctx = self.ctx
        if chans is None:
            chans = await ctx.db.list_channels()
        busy = (lambda: False) if explicit else ctx.lock.locked
        reports: list = []
        for ch in chans:
            if busy():
                break
            fallback = deleter = None
            if clean and ctx.userbot is not None:
                fallback, deleter = await ctx.userbot.fallback_for(ch)

            async def prog(rep, ch=ch):
                if progress is not None:
                    await progress(ch, rep)

            try:
                async with self._busy:
                    opts = self.options(deep=explicit, clean=clean, fallback=fallback)
                    rep = await sync_channel(ctx.client, ctx.db, ch, opts, busy=busy, progress=prog)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("looking at channel %s failed", ch.id)
                rep = SyncReport(channel=ch, error=f"{type(e).__name__}: {str(e)[:120]}")
            finally:
                if deleter is not None:
                    await deleter.close()
            await self._after(ch, rep)
            reports.append(rep)
        return reports

    # ----------------------------------------------------------------------------- live: what updates say
    def _touch(self) -> None:
        self._stamp += 1
        self._wake.set()

    def note_new(self, cid: int, msg) -> None:
        """A post appeared in a channel. The bot's own posts are skipped (it saves them itself)."""
        if not self.enabled or self.ctx.lock.locked() or getattr(msg, "out", False):
            return
        self._news.setdefault(cid, set()).add(msg.id)
        self._touch()

    def note_edit(self, cid: int, msg) -> None:
        if not self.enabled or self.ctx.lock.locked():
            return
        self._edits.setdefault(cid, set()).add(msg.id)
        self._touch()

    async def note_deleted(self, cid: int, ids) -> None:
        """Posts were deleted in a channel: they leave My posts at once (nothing to read from Telegram for that)."""
        if not self.enabled:
            return
        ch = await self.channel(cid)
        if ch is None:
            return
        for pending in (self._edits, self._news):
            if cid in pending:
                pending[cid].difference_update(ids)
        n = await self.ctx.db.forget_posts(cid, list(ids))
        if n:
            log.info("%s post(s) were deleted in channel %s: removed from My posts", n, cid)

    async def on_service(self, cid: int, msg) -> None:
        """A service message appeared: delete it."""
        if not self.ctx.cfg.delete_service_messages:
            return
        ch = await self.channel(cid)
        if ch is None:
            return
        out = await remove(self.ctx.client, peer_of(ch), [msg.id])
        if out.error is None:
            log.info("removed the “%s” notice %s from channel %s", action_label(msg), msg.id, cid)
            return
        log.warning("could not delete the “%s” notice %s in channel %s: %s", action_label(msg), msg.id, cid, out.error_name)
        if out.error_name in RIGHTS_ERRORS:
            await self.tell_owners(ch, "rights", self._rights_text(ch, out.error_name))

    def _pending(self) -> bool:
        return bool(self._edits or self._news)

    async def flush_events(self) -> None:
        """Work off the queue once it has been quiet for a moment (and no long job is running)."""
        first = time.monotonic()
        while self._pending():
            stamp = self._stamp
            await asyncio.sleep(self.debounce)
            if stamp != self._stamp and time.monotonic() - first < self.max_debounce:
                continue  # more is still coming in: let it settle
            if self.ctx.lock.locked():
                await asyncio.sleep(self.lock_wait)
                continue
            edits, news = self._edits, self._news
            self._edits, self._news = {}, {}
            await self._process(edits, news)
            first = time.monotonic()

    def _requeue(self, pending: dict, cid: int, mid: int) -> None:
        key = (cid, mid)
        self._tries[key] = self._tries.get(key, 0) + 1
        if self._tries[key] > self.max_tries:
            self._tries.pop(key, None)
            return
        pending.setdefault(cid, set()).add(mid)

    async def _process(self, edits: dict, news: dict) -> None:
        ctx = self.ctx
        for cid in sorted(set(edits) | set(news)):
            ch = await self.channel(cid)
            if ch is None:
                continue
            e_ids, n_ids = set(edits.get(cid, ())), set(news.get(cid, ()))
            opts = self.options(grace=self.rt_grace)
            try:
                async with self._busy:
                    if len(e_ids | n_ids) > self.max_ids:  # a burst: one look at the whole channel is cheaper
                        rep = await sync_channel(ctx.client, ctx.db, ch, opts, busy=ctx.lock.locked)
                    else:
                        rep = await sync_ids(
                            ctx.client, ctx.db, ch, e_ids | n_ids, opts, adopt_ids=n_ids, busy=ctx.lock.locked
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("looking at the changes in channel %s failed", cid)
                rep = SyncReport(channel=ch, error=f"{type(e).__name__}: {str(e)[:120]}")
            await self._after(ch, rep)
            if rep.busy:  # a long job started in the meantime: let the next timed check have it
                continue
            for mid in set(rep.young):
                self._requeue(self._edits, cid, mid)
            for mid in set(rep.young_new):
                self._requeue(self._news, cid, mid)
            for mid in (e_ids | n_ids) - set(rep.young) - set(rep.young_new):
                self._tries.pop((cid, mid), None)
