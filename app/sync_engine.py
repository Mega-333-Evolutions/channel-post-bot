"""Keeps My posts in step with the channels: what admins do outside the bot is noticed and recorded.

What a look at a channel does
  * every post the bot has saved is read back from Telegram: a post that is gone is removed here, a post that was edited
    gets its saved copy updated (text, formatting, buttons, media);
  * messages that appeared since the last look are saved as new posts (source "adopted"). The very first look only
    notes where the channel stands: this follows changes, it does not import the old posts;
  * service messages ("channel name changed", "pinned a message", video chat ...) found on the way are deleted.

Safety rules
  * posts the bot touched a moment ago, and messages that appeared a moment ago, are left for the next look (the bot may
    still be saving them);
  * a saved copy is only changed if nobody changed it since it was read (optimistic check in the database);
  * buttons that are missing in the channel are never removed from the saved copy (Telegram sometimes hides a keyboard
    for a while - the saved buttons are what "Check buttons" restores from);
  * if EVERY saved post of a channel looks deleted, nothing is removed: more likely the bot lost access.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from telethon import errors, functions, types

from .db import as_utc, utcnow
from .replace_engine import fetch_messages
from .repost_engine import delete_batch
from .tgutil import classify_media, explain_rpc, flood_retry, media_from_ref, media_info, peer_of, ser_entities, ser_markup

log = logging.getLogger(__name__)

CHUNK = 100  # Telegram returns at most 100 messages per request
GRACE = 120.0  # seconds a post or message must be old before it is judged
PAUSE = 0.25  # between two requests of a scan
SLACK_CAP = 300  # a quick look reads this many ids beyond the most likely end of the new messages
DEEP_EVERY = 24 * 3600  # the automatic looks read every possible id at least this often
MIN_FOR_ANOMALY = 5
MAX_PROBE = 300  # requests spent finding the newest message the first time (30 000 ids)


# ----------------------------------------------------------------------------------------------- options
@dataclass
class SyncOptions:
    deep: bool = False  # read every possible id for new messages, not only the likely ones
    clean: bool = False  # also sweep the whole history for old service messages
    delete_services: bool = True
    adopt: bool = True  # save new posts of others
    grace: float = GRACE
    pause: float = PAUSE
    ignore: Any = field(default_factory=set)  # (channel id, message id) of posts the owner told the bot to forget
    fallback: Optional[Callable[[list], Awaitable[Any]]] = None  # deletes with the userbot (only used by `clean`)


@dataclass
class SyncReport:
    channel: Any
    phase: str = ""
    checked: int = 0  # saved posts compared with the channel
    deleted: list = field(default_factory=list)  # message ids that were gone: removed from My posts
    edited: list = field(default_factory=list)  # message ids whose saved copy was brought up to date
    refreshed: int = 0  # saved copies corrected for trifles (a trailing space ...)
    adopted: list = field(default_factory=list)  # message ids of new posts that were saved
    unsupported: int = 0  # new messages of a kind My posts can't show (polls, stickers ...)
    buttons_missing: list = field(default_factory=list)  # saved buttons that the channel shows no keyboard for
    services_deleted: int = 0
    services_failed: int = 0
    services_error: Optional[str] = None  # name of the refusal
    young: list = field(default_factory=list)  # saved posts the bot touched a moment ago: judge them later
    young_new: list = field(default_factory=list)  # new messages that appeared a moment ago: save them later
    changed_meanwhile: int = 0  # saved posts that were changed through the bot while we looked (left alone)
    first_look: bool = False
    busy: bool = False  # a long job started: the look was cut short
    anomaly: Optional[str] = None
    error: Optional[str] = None
    scanned_to: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.deleted or self.edited or self.adopted or self.services_deleted)


# ------------------------------------------------------------------------------------- comparing a post
def age(dt, now) -> float:
    """Seconds since `dt` (never negative); unknown = very old."""
    if dt is None:
        return float("inf")
    return max(0.0, (now - as_utc(dt)).total_seconds())


def media_key(media) -> Optional[tuple]:
    """Which photo / file a message carries (its id - the file reference changes from fetch to fetch)."""
    if isinstance(media, types.MessageMediaPhoto) and getattr(media, "photo", None):
        return ("photo", media.photo.id)
    if isinstance(media, types.MessageMediaDocument) and getattr(media, "document", None):
        return ("doc", media.document.id)
    return None


def stored_media_key(ref: Optional[str]) -> Optional[tuple]:
    if not ref:
        return None
    try:
        return media_key(media_from_ref(ref))
    except Exception:
        return ("unreadable", ref[:40])  # differs from anything real: the saved reference gets replaced


def live_fields(m) -> dict:
    """The saved-post fields of a message as it is in the channel now."""
    kind, ref = media_info(m)
    return {
        "text": m.message or "",
        "entities": ser_entities(m.entities),
        "buttons": ser_markup(m.reply_markup),
        "media_kind": kind,
        "media_file_id": ref,
    }


def message_supported(m) -> bool:
    """Something My posts can show and edit: text, or a photo / video / GIF / audio / file."""
    return bool((m.message or "").strip()) or classify_media(m) is not None


def adopt_fields(m) -> dict:
    f = live_fields(m)
    f["link_preview"] = isinstance(getattr(m, "media", None), types.MessageMediaWebPage)
    return f


@dataclass
class Diff:
    fields: dict = field(default_factory=dict)  # what to save
    buttons_missing: bool = False
    quiet: bool = False  # only a trifle differs (surrounding spaces): fix it without counting it as an edit


def diff_post(p, m) -> Diff:
    """What differs between the saved post `p` and the live message `m`."""
    live = live_fields(m)
    fields: dict = {}
    if (p.text or "") != live["text"]:
        fields["text"] = live["text"]
    if list(p.entities or []) != live["entities"]:
        fields["entities"] = live["entities"]
    # media: replaced media is taken over; media that seems to have vanished is not (a message can't lose its media)
    if live["media_kind"] is not None:
        if p.media_kind != live["media_kind"] or stored_media_key(p.media_file_id) != media_key(m.media):
            fields["media_kind"], fields["media_file_id"] = live["media_kind"], live["media_file_id"]
    missing = False
    if live["buttons"]:
        if list(p.buttons or []) != live["buttons"]:
            fields["buttons"] = live["buttons"]
    elif p.buttons:
        missing = True
    quiet = set(fields) == {"text"} and (p.text or "").strip() == live["text"].strip()
    return Diff(fields, missing, quiet)


# --------------------------------------------------------------------------------------- talking to Telegram
async def channel_pts(client, peer) -> tuple:
    """(the channel's event counter, None) or (None, why not). The counter is never lower than the newest message id."""
    try:
        full = await flood_retry(lambda: client(functions.channels.GetFullChannelRequest(peer)))
    except errors.RPCError as e:
        return None, explain_rpc(e)
    pts = getattr(getattr(full, "full_chat", None), "pts", None)
    if pts is None:
        return None, "Telegram did not say how far the channel has come."
    return int(pts), None


def is_gone(m) -> bool:
    return m is None or isinstance(m, types.MessageEmpty)


async def find_top(client, peer, pts: int, floor: int, *, pause: float = PAUSE) -> int:
    """The newest message id that exists. Only the event counter is known (never lower), so look downwards from it;
    `floor` (the newest saved post) is where the looking can stop."""
    hi = pts
    for _ in range(MAX_PROBE):
        if hi <= floor:
            break
        lo = max(floor + 1, hi - CHUNK + 1)
        msgs = await fetch_messages(client, peer, list(range(lo, hi + 1)))
        alive = [m.id for m in msgs if not is_gone(m)]
        if alive:
            return max(alive)
        hi = lo - 1
        await asyncio.sleep(pause)
    return max(floor, hi)


async def delete_services(client, peer, ids: list, rep: SyncReport, opts: SyncOptions) -> None:
    """Delete service messages (bot first; `opts.fallback` for what it may not delete) and count what happened."""
    ids = sorted(set(ids))
    for i in range(0, len(ids), CHUNK):
        part = ids[i : i + CHUNK]
        out = await delete_batch(client, peer, part, opts.fallback)
        rep.services_deleted += len(out.gone)
        left = len(part) - len(out.gone)
        if left:
            rep.services_failed += left
            rep.services_error = rep.services_error or out.bot_error or out.fallback_error or "NotDeleted"


# ------------------------------------------------------------------------------------- the three steps
async def _save_edits(db, rep: SyncReport, edits: list, busy) -> None:
    for p, d in edits:
        if busy():
            rep.busy = True
            return
        if await db.sync_update_post(p.id, p.updated_at, d.fields):
            if d.quiet:
                rep.refreshed += 1
            else:
                rep.edited.append(p.message_id)
        else:
            rep.changed_meanwhile += 1


async def _adopt(db, ch, rep: SyncReport, fresh: list, opts: SyncOptions, busy, now) -> None:
    """Save messages the bot does not know (ascending ids); too fresh ones are left for the next look."""
    known: set = set()
    ids = [m.id for m in fresh]
    for i in range(0, len(ids), 500):
        known |= {p.message_id for p in await db.posts_at(ch.id, ids[i : i + 500])}
    todo = []
    for m in fresh:
        if m.id in known or (ch.id, m.id) in opts.ignore:
            continue
        if age(m.date, now) < opts.grace:
            rep.young_new.append(m.id)
        elif not message_supported(m):
            rep.unsupported += 1
        else:
            todo.append(m)
    for m in todo:
        if busy():
            rep.busy = True
            return
        _, created = await db.adopt_message(ch.id, m.id, adopt_fields(m), sent_at=m.date)
        if created:
            rep.adopted.append(m.id)


async def _compare_saved(client, db, ch, rep: SyncReport, opts: SyncOptions, busy, progress) -> None:
    """Every saved post against the channel."""
    rep.phase = "comparing the saved posts"
    peer = peer_of(ch)
    posts = await db.sent_posts(ch.id)  # the saved copies are read BEFORE the channel is, never after
    now = utcnow()
    judge = []
    for p in posts:
        if min(age(p.updated_at, now), age(p.sent_at, now)) < opts.grace:
            rep.young.append(p.message_id)
        else:
            judge.append(p)
    rep.checked = len(judge)
    by_id = {p.message_id: p for p in judge}
    ids = sorted(by_id)
    gone, edits = [], []
    for i in range(0, len(ids), CHUNK):
        if busy():
            rep.busy = True
            return
        chunk = ids[i : i + CHUNK]
        msgs = await fetch_messages(client, peer, chunk)
        for mid, m in zip(chunk, msgs):
            p = by_id[mid]
            if is_gone(m):
                gone.append(p)
            elif isinstance(m, types.MessageService):
                continue  # not a post any more (cannot happen: ids are never reused)
            else:
                d = diff_post(p, m)
                if d.buttons_missing:
                    rep.buttons_missing.append(mid)
                if d.fields:
                    edits.append((p, d))
        rep.scanned_to = chunk[-1]
        if progress:
            await progress(rep)
        await asyncio.sleep(opts.pause)
    if gone:
        if len(ids) >= MIN_FOR_ANOMALY and len(gone) == len(ids):
            rep.anomaly = (
                f"All {len(ids)} saved posts look deleted, so none was removed here - "
                "check that the bot is still an admin of the channel."
            )
        else:
            gone_ids = [p.message_id for p in gone]
            await db.forget_posts(ch.id, gone_ids)
            rep.deleted.extend(gone_ids)
    await _save_edits(db, rep, edits, busy)


async def _sweep_services(client, ch, rep: SyncReport, opts: SyncOptions, pts: int, busy, progress) -> None:
    """The whole history, once: every service message that is still there goes."""
    rep.phase = "removing old service messages"
    peer = peer_of(ch)
    a = 1
    while a <= pts:
        if busy():
            rep.busy = True
            return
        hi = min(a + CHUNK - 1, pts)
        msgs = await fetch_messages(client, peer, list(range(a, hi + 1)))
        ids = [m.id for m in msgs if isinstance(m, types.MessageService)]
        if ids:
            await delete_services(client, peer, ids, rep, opts)
        rep.scanned_to = hi
        if progress:
            await progress(rep)
        a = hi + 1
        await asyncio.sleep(opts.pause)


async def _look_for_new(client, db, ch, rep: SyncReport, opts: SyncOptions, state, pts: int, busy, progress) -> None:
    """Messages that appeared since the last look: new posts are saved, service messages deleted."""
    rep.phase = "looking for new posts"
    peer = peer_of(ch)
    now = utcnow()
    if state is None:  # the first look: only note where the channel stands
        floor = await db.max_saved_id(ch.id)
        top = await find_top(client, peer, pts, floor, pause=opts.pause)
        rep.first_look, rep.scanned_to = True, top
        await db.save_sync(ch.id, last_top=top, last_pts=pts, deferred=0, deep=True)
        return
    deep = opts.deep or age(state.last_deep, now) > DEEP_EVERY
    if pts == state.last_pts and not state.deferred and not deep:
        return  # not a single event since the last look: a quick look would read exactly what it read then
    delta = max(0, pts - state.last_pts)
    slack = min(max(0, state.last_pts - state.last_top), SLACK_CAP)
    soft = pts if deep else min(pts, state.last_top + delta + slack)
    a, highest, had_live = state.last_top + 1, state.last_top, True
    fresh, services = [], []
    while a <= pts:
        if a > soft and not had_live:
            break  # past the likely end and the last stretch was empty
        if busy():
            rep.busy = True
            return
        hi = min(a + CHUNK - 1, pts)
        msgs = await fetch_messages(client, peer, list(range(a, hi + 1)))
        had_live = False
        for m in msgs:
            if is_gone(m):
                continue
            had_live = True
            highest = max(highest, m.id)
            if isinstance(m, types.MessageService):
                services.append(m)
            else:
                fresh.append(m)
        rep.scanned_to = hi
        if progress:
            await progress(rep)
        a = hi + 1
        await asyncio.sleep(opts.pause)
    services_ids = [m.id for m in services]
    if opts.delete_services and services_ids:
        await delete_services(client, peer, services_ids, rep, opts)
    if opts.adopt and fresh:
        await _adopt(db, ch, rep, fresh, opts, busy, now)
        if rep.busy:
            return
    new_top = highest
    if rep.young_new:  # these have to be looked at again: don't move the mark past them
        new_top = min(new_top, min(rep.young_new) - 1)
    await db.save_sync(
        ch.id, last_top=max(state.last_top, new_top), last_pts=pts, deferred=len(rep.young_new), deep=deep
    )


# ----------------------------------------------------------------------------------------- entry points
async def sync_channel(client, db, ch, opts: Optional[SyncOptions] = None, *, busy=None, progress=None) -> SyncReport:
    """One full look at a channel (see the module text). Never raises Telegram's refusals: they end up in `report.error`."""
    opts = opts or SyncOptions()
    busy = busy or (lambda: False)
    rep = SyncReport(channel=ch)
    pts, err = await channel_pts(client, peer_of(ch))
    if pts is None:
        rep.error = err
        return rep
    try:
        state = await db.get_sync(ch.id)
        await _compare_saved(client, db, ch, rep, opts, busy, progress)
        if opts.clean and not rep.busy:
            await _sweep_services(client, ch, rep, opts, pts, busy, progress)
        if not rep.busy:
            await _look_for_new(client, db, ch, rep, opts, state, pts, busy, progress)
    except errors.RPCError as e:
        rep.error = explain_rpc(e)
    return rep


async def sync_ids(client, db, ch, ids, opts: Optional[SyncOptions] = None, *, adopt_ids=(), busy=None) -> SyncReport:
    """A look at these messages only - the ones an update told about. `adopt_ids`: the ones that are new posts, so a
    message the bot does not know may be saved (an edited message the bot does not know is history: left alone)."""
    opts = opts or SyncOptions()
    busy = busy or (lambda: False)
    rep = SyncReport(channel=ch)
    peer = peer_of(ch)
    ids = sorted(set(ids))
    adopt_set = set(adopt_ids)
    try:
        rows = {p.message_id: p for p in await db.posts_at(ch.id, ids)}  # saved copies first, the channel second
        now = utcnow()
        gone, edits, fresh, services = [], [], [], []
        for i in range(0, len(ids), CHUNK):
            if busy():
                rep.busy = True
                return rep
            chunk = ids[i : i + CHUNK]
            msgs = await fetch_messages(client, peer, chunk)
            for mid, m in zip(chunk, msgs):
                row = rows.get(mid)
                recent = row is not None and min(age(row.updated_at, now), age(row.sent_at, now)) < opts.grace
                if is_gone(m):
                    if row is not None and not recent:
                        gone.append(row)
                elif isinstance(m, types.MessageService):
                    services.append(mid)
                elif row is not None:
                    if recent:
                        rep.young.append(mid)
                        continue
                    d = diff_post(row, m)
                    if d.buttons_missing:
                        rep.buttons_missing.append(mid)
                    if d.fields:
                        edits.append((row, d))
                elif mid in adopt_set:
                    fresh.append(m)
        if opts.delete_services and services:
            await delete_services(client, peer, services, rep, opts)
        if gone:
            gone_ids = [p.message_id for p in gone]
            await db.forget_posts(ch.id, gone_ids)
            rep.deleted.extend(gone_ids)
        await _save_edits(db, rep, edits, busy)
        if opts.adopt and fresh and not rep.busy:
            await _adopt(db, ch, rep, fresh, opts, busy, now)
    except errors.RPCError as e:
        rep.error = explain_rpc(e)
    return rep
