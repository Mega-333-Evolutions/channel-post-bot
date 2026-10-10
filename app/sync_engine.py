"""Keeps My posts in step with the channels: what admins do outside the bot is noticed and recorded.

What a look at a channel does
  * every post the bot has saved is read back from Telegram: a post that is gone is removed here, a post that was edited
    gets its saved copy updated (text, formatting, buttons, media);
  * messages that appeared since the last look are saved as new posts (source "adopted");
  * the posts that were in the channel BEFORE the bot was added are read into My posts too, oldest first, up to the
    newest message that existed at the first look. Where the reading stopped is saved, so a long channel takes a few
    looks. Posts the owner told the bot to forget are skipped for good;
  * service messages ("channel name changed", "pinned a message", video chat ...) found on the way are deleted.

Safety rules
  * posts the bot touched a moment ago, and messages that appeared a moment ago, are left for the next look (the bot may
    still be saving them);
  * a saved copy is only changed if nobody changed it since it was read (optimistic check in the database);
  * buttons that are missing in the channel are never removed from the saved copy (Telegram sometimes hides a keyboard
    for a while - the saved buttons are what "Check buttons" restores from);
  * if EVERY saved post of a channel looks deleted, nothing is removed: more likely the bot lost access (unless the
    channel's event counter moved by at least that many events, which is what really deleting them looks like);
  * what TELEGRAM does to a channel is not an admin's edit: a channel it restricts (a copyright strike turns every post
    into "This message couldn't be displayed on your device due to copyright infringement"), a single post it holds
    back, a notice shown instead of a post, or many posts that suddenly all show one and the same text are left exactly
    as saved - nothing is edited, removed or added for them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from telethon import errors, types

from .db import as_utc, utcnow
from .replace_engine import fetch_messages
from .repost_engine import delete_batch
from .restrictions import message_restriction, norm_text, probe_channel
from .tgutil import classify_media, explain_rpc, media_from_ref, media_info, peer_of, ser_entities, ser_markup

log = logging.getLogger(__name__)

CHUNK = 100  # Telegram returns at most 100 messages per request
GRACE = 120.0  # seconds a post or message must be old before it is judged
PAUSE = 0.25  # between two requests of a scan
SLACK_CAP = 300  # a quick look reads this many ids beyond the most likely end of the new messages
DEEP_EVERY = 24 * 3600  # the automatic looks read every possible id at least this often
MIN_FOR_ANOMALY = 5
MASS_SAME = 5  # this many posts that suddenly show the very same new text are not edits by a person
MAX_PROBE = 300  # requests spent finding the newest message the first time (30 000 ids)


# ----------------------------------------------------------------------------------------------- options
@dataclass
class SyncOptions:
    deep: bool = False  # read every possible id for new messages, not only the likely ones
    delete_services: bool = True
    adopt: bool = True  # save new posts of others
    history: bool = True  # also read the posts from before the bot was added into My posts
    history_seconds: Optional[float] = None  # ... for at most this long per look (None: as long as it takes)
    grace: float = GRACE
    pause: float = PAUSE
    ignore: Any = field(default_factory=set)  # (channel id, message id) of posts the owner told the bot to forget


@dataclass
class SyncReport:
    channel: Any
    phase: str = ""
    checked: int = 0  # saved posts compared with the channel
    deleted: list = field(default_factory=list)  # message ids that were gone: removed from My posts
    edited: list = field(default_factory=list)  # message ids whose saved copy was brought up to date
    refreshed: int = 0  # saved copies corrected for trifles (a trailing space ...)
    adopted: list = field(default_factory=list)  # message ids of new posts that were saved
    imported: int = 0  # older posts (from before the bot was added) that were added to My posts
    history_unsupported: int = 0  # older messages My posts can't hold (polls, stickers ...)
    history_top: int = 0  # the newest message that counts as older (0: the older posts were not looked at)
    history_to: int = 0  # the older posts have been read up to this message id
    history_done: bool = False  # ... and that was all of them
    unsupported: int = 0  # new messages of a kind My posts can't show (polls, stickers ...)
    buttons_missing: list = field(default_factory=list)  # saved buttons that the channel shows no keyboard for
    services_deleted: int = 0
    services_failed: int = 0
    services_error: Optional[str] = None  # name of the refusal
    restricted: list = field(default_factory=list)  # message ids Telegram holds back (copyright ...): left as saved
    restricted_why: Optional[str] = None  # what Telegram says about them
    restricted_channel: Optional[str] = None  # the whole channel is restricted: nothing was looked at
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
        return bool(self.deleted or self.edited or self.adopted or self.imported or self.services_deleted)


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


# ------------------------------------------------------------------- what Telegram itself does to a channel
MASS_NOTE = "many posts suddenly show one and the same text - that is Telegram's notice, not an edit"


def split_mass_text(edits: list) -> tuple:
    """(edits to save, edits to hold back). Five or more saved posts that had five different texts and now all show the
    very same text at once: nobody edits like that - Telegram does it to a channel it restricts."""
    groups: dict = {}
    for item in edits:
        new = norm_text(item[1].fields.get("text"))
        if new:
            groups.setdefault(new, []).append(item)
    bad: set = set()
    for items in groups.values():
        if len(items) >= MASS_SAME and len({norm_text(p.text) for p, _ in items}) >= MASS_SAME:
            bad.update(id(i) for i in items)
    return [i for i in edits if id(i) not in bad], [i for i in edits if id(i) in bad]


def hold_back(rep: "SyncReport", ids, why: Optional[str]) -> None:
    rep.restricted.extend(ids)
    rep.restricted_why = rep.restricted_why or why


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


async def delete_services(client, peer, ids: list, rep: SyncReport) -> None:
    """Delete service messages and count what happened (the bot only: Telegram may refuse the old ones)."""
    ids = sorted(set(ids))
    for i in range(0, len(ids), CHUNK):
        part = ids[i : i + CHUNK]
        out = await delete_batch(client, peer, part)
        rep.services_deleted += len(out.gone)
        left = len(part) - len(out.gone)
        if left:
            rep.services_failed += left
            rep.services_error = rep.services_error or out.bot_error or "NotDeleted"


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
    forgotten = await db.ignored_ids(ch.id) if fresh else set()
    todo = []
    for m in fresh:
        if m.id in known or m.id in forgotten or (ch.id, m.id) in opts.ignore:
            continue
        why = message_restriction(m)
        if why:  # Telegram holds it back: what it shows now is not what the post says
            hold_back(rep, [m.id], why)
        elif age(m.date, now) < opts.grace:
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


async def _compare_saved(client, db, ch, rep: SyncReport, opts: SyncOptions, busy, progress, moved=None) -> None:
    """Every saved post against the channel. `moved`: how far the channel's event counter has come since the last look
    (None: unknown) - deleting posts is an event each, which tells "everything was deleted" from "I can't see anything"."""
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
                why = message_restriction(m, p.text)
                if why:  # Telegram's doing, not an admin's: the saved copy stays exactly as it is
                    hold_back(rep, [mid], why)
                    continue
                d = diff_post(p, m)
                if d.buttons_missing:
                    rep.buttons_missing.append(mid)
                if d.fields:
                    edits.append((p, d))
        rep.scanned_to = chunk[-1]
        if progress:
            await progress(rep)
        await asyncio.sleep(opts.pause)
    edits, held = split_mass_text(edits)
    if held:
        hold_back(rep, [p.message_id for p, _ in held], MASS_NOTE)
    if gone:
        really = moved is not None and moved >= len(gone)  # at least one event for each post that vanished
        if len(ids) >= MIN_FOR_ANOMALY and len(gone) == len(ids) and not really:
            rep.anomaly = (
                f"All {len(ids)} saved posts look deleted, so none was removed here - "
                "check that the bot is still an admin of the channel."
            )
        else:
            gone_ids = [p.message_id for p in gone]
            await db.forget_posts(ch.id, gone_ids)
            rep.deleted.extend(gone_ids)
    await _save_edits(db, rep, edits, busy)


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
        await delete_services(client, peer, services_ids, rep)
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


async def _import_history(client, db, ch, rep: SyncReport, opts: SyncOptions, busy, progress, state=None) -> None:
    """The posts that were in the channel before the bot was added are read into My posts: from message 1 up to the newest
    message that existed at the first look (what came after is the business of _look_for_new). The place where the
    reading stopped is saved - a long channel takes a few looks, and a restart loses nothing. `state`: where the look
    before this one stopped (None at the very first look: the mark that look has just set is used)."""
    hist = await db.get_history(ch.id)
    if hist is None:
        base = state if state is not None else await db.get_sync(ch.id)
        if base is None:
            return
        hist = await db.start_history(ch.id, base.last_top)
    if hist.done:
        return
    rep.phase = "reading the older posts"
    peer = peer_of(ch)
    began = time.monotonic()
    forgotten = await db.ignored_ids(ch.id)
    rep.history_top = hist.top
    a = hist.next_id
    rep.history_to = a - 1
    while a <= hist.top:
        if busy():
            rep.busy = True
            return
        if opts.history_seconds is not None and time.monotonic() - began > opts.history_seconds:
            return  # the next look goes on from here
        hi = min(a + CHUNK - 1, hist.top)
        msgs = await fetch_messages(client, peer, list(range(a, hi + 1)))
        now = utcnow()
        stop = None
        todo = []
        for m in msgs:
            if is_gone(m) or isinstance(m, types.MessageService):
                continue
            if age(m.date, now) < opts.grace:  # the bot may still be saving it: the next look reads it
                stop = m.id
                break
            if m.id in forgotten or (ch.id, m.id) in opts.ignore:
                continue
            why = message_restriction(m)
            if why:
                hold_back(rep, [m.id], why)
            elif not message_supported(m):
                rep.history_unsupported += 1
            else:
                todo.append(m)
        known = {p.message_id for p in await db.posts_at(ch.id, [m.id for m in todo])}
        made = await db.adopt_many(ch.id, [(m.id, adopt_fields(m), m.date) for m in todo if m.id not in known])
        rep.imported += len(made)
        a = hi + 1 if stop is None else stop
        rep.history_done = stop is None and a > hist.top
        await db.save_history(ch.id, next_id=a, imported=len(made), done=rep.history_done)
        rep.history_to = rep.scanned_to = a - 1
        if stop is not None:
            return
        if progress:
            await progress(rep)
        await asyncio.sleep(opts.pause)
    rep.history_done = True  # (also the case when the channel had nothing to read)


# ----------------------------------------------------------------------------------------- entry points
async def sync_channel(client, db, ch, opts: Optional[SyncOptions] = None, *, busy=None, progress=None) -> SyncReport:
    """One full look at a channel (see the module text). Never raises Telegram's refusals: they end up in `report.error`."""
    opts = opts or SyncOptions()
    busy = busy or (lambda: False)
    rep = SyncReport(channel=ch)
    pts, restricted, err = await probe_channel(client, peer_of(ch))
    if pts is None:
        rep.error = err
        return rep
    if restricted:  # Telegram restricted the channel (a copyright strike ...): My posts stay untouched
        rep.restricted_channel = restricted
        return rep
    try:
        state = await db.get_sync(ch.id)
        moved = None if state is None else max(0, pts - state.last_pts)
        await _compare_saved(client, db, ch, rep, opts, busy, progress, moved)
        if not rep.busy:
            await _look_for_new(client, db, ch, rep, opts, state, pts, busy, progress)
        if not rep.busy and opts.history:
            await _import_history(client, db, ch, rep, opts, busy, progress, state)
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
                    why = message_restriction(m, row.text)
                    if why:  # Telegram's doing, not an admin's: the saved copy stays exactly as it is
                        hold_back(rep, [mid], why)
                        continue
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
            await delete_services(client, peer, services, rep)
        edits, held = split_mass_text(edits)
        if held:
            hold_back(rep, [p.message_id for p, _ in held], MASS_NOTE)
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
