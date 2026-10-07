"""/replace engine: scan channels message by message, rewrite links, log every change, undo."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from telethon import errors, functions, types

from .handleswap import compute_handle_changes
from .linkswap import compute_message_changes
from .tgutil import (
    build_markup,
    de_entities,
    edit_raw,
    flood_retry,
    media_info,
    peer_of,
    ser_entities,
    ser_markup,
    state_of,
)

log = logging.getLogger(__name__)

CHUNK = 100  # Telegram returns at most 100 messages per request
FALLBACK_EMPTY_BATCHES = 15  # used only when the newest message id can't be determined
ABORT_AFTER = 5  # stop a channel after this many identical Telegram refusals in a row


@dataclass
class ScanOptions:
    old: str
    new: str
    include_typed: bool = True
    include_posts: bool = False
    last: Optional[int] = None
    mode: str = "links"  # "links": the username inside t.me links (/replace); "handles": '@name' written in a post (/handleswap)


def compute_changes(m, opts: ScanOptions) -> tuple:
    """What this run would change in message `m`: (MsgChange or None, skipped)."""
    if opts.mode == "handles":
        return compute_handle_changes(m, opts.old, opts.new)
    return compute_message_changes(
        m, opts.old, opts.new, include_typed=opts.include_typed, include_posts=opts.include_posts
    )


@dataclass
class ChangeInfo:
    message_id: int
    buttons: int = 0
    links: int = 0
    typed: int = 0
    mine: bool = False
    callbacks: bool = False


@dataclass
class ChannelScan:
    channel: Any
    can_edit_others: bool = True
    top: int = 0
    first: int = 1
    scanned: int = 0
    infos: dict = field(default_factory=dict)
    skipped_post_links: int = 0
    not_editable: int = 0
    error: Optional[str] = None
    rights: Any = None

    @property
    def total(self) -> int:
        return len(self.infos)

    @property
    def foreign(self) -> int:
        return sum(1 for i in self.infos.values() if not i.mine)


@dataclass
class ApplyResult:
    edited: int = 0
    unchanged: int = 0
    missing: int = 0
    no_perm: int = 0
    failed: list = field(default_factory=list)
    aborted: Optional[str] = None  # error name that made us stop early


@dataclass
class UndoResult:
    restored: int = 0
    failed: list = field(default_factory=list)


async def fetch_messages(client, peer, ids: list) -> list:
    res = await flood_retry(lambda: client.get_messages(peer, ids=ids))
    return list(res)


async def get_top_id(client, peer) -> Optional[int]:
    """Upper bound for the newest message id: the channel's event counter (pts) is never lower."""
    try:
        full = await flood_retry(lambda: client(functions.channels.GetFullChannelRequest(peer)))
        pts = getattr(full.full_chat, "pts", None)
        if pts:
            return int(pts)
    except Exception as e:
        log.info("could not read channel counter: %s", e)
    return None


def is_skippable(m) -> bool:
    return m is None or isinstance(m, (types.MessageEmpty, types.MessageService))


async def scan_channel(
    client,
    ch,
    opts: ScanOptions,
    *,
    can_edit_others: bool = True,
    mine_ids: Optional[set] = None,
    progress: Optional[Callable[[ChannelScan], Awaitable[None]]] = None,
) -> ChannelScan:
    scan = ChannelScan(channel=ch, can_edit_others=can_edit_others)
    peer = peer_of(ch)
    mine_ids = mine_ids or set()
    top = await get_top_id(client, peer)
    first = 1
    if opts.last and top:
        first = max(1, top - opts.last + 1)
    scan.first, scan.top = first, top or 0
    a, empty_run = first, 0
    while True:
        if top is not None and a > top:
            break
        hi = a + CHUNK - 1 if top is None else min(a + CHUNK - 1, top)
        msgs = await fetch_messages(client, peer, list(range(a, hi + 1)))
        found = False
        for m in msgs:
            if m is None or isinstance(m, types.MessageEmpty):
                continue
            found = True
            if isinstance(m, types.MessageService):
                continue
            change, skipped = compute_changes(m, opts)
            scan.skipped_post_links += skipped
            if not change:
                continue
            mine = change.mine or m.id in mine_ids
            if not mine and not can_edit_others:
                scan.not_editable += 1
                continue
            scan.infos[m.id] = ChangeInfo(
                m.id, change.n_buttons, change.n_links, change.n_typed, mine, change.callbacks
            )
        scan.scanned = hi
        if top is None:
            empty_run = 0 if found else empty_run + 1
            if empty_run >= FALLBACK_EMPTY_BATCHES:
                break
        if progress:
            await progress(scan)
        a = hi + 1
    if top is None:
        scan.top = scan.scanned
    return scan


def preview_flag(m):
    if isinstance(m.media, types.MessageMediaWebPage):
        return True
    return False if m.media is None else None


async def _do_edit(client, peer, m, change, markup, preview) -> None:
    if change.text_changed or change.entities_changed:
        await edit_raw(
            client, peer, m.id, text=change.new_text, entities=change.new_entities, markup=markup, preview=preview
        )
    else:  # only button links changed: leave the text untouched
        await edit_raw(client, peer, m.id, markup=markup)


async def apply_channel(
    client,
    db,
    scan: ChannelScan,
    opts: ScanOptions,
    batch_id: str,
    user_id: int,
    *,
    edit_delay: float = 1.2,
    progress: Optional[Callable[[ApplyResult], Awaitable[None]]] = None,
) -> ApplyResult:
    ch = scan.channel
    peer = peer_of(ch)
    mine_ids = await db.mine_message_ids(ch.id)
    res = ApplyResult()
    ids = sorted(scan.infos)
    buf: list = []
    streak, last_name = 0, None

    async def flush() -> None:
        nonlocal buf
        if buf:
            batch, buf = buf, []
            await db.record_changes(batch_id, ch.id, batch, user_id)

    try:
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            msgs = await fetch_messages(client, peer, chunk)
            for mid, m in zip(chunk, msgs):
                if is_skippable(m):
                    res.missing += 1
                    continue
                # re-check against the live message: it may have been edited since the scan
                change, _ = compute_changes(m, opts)
                if not change:
                    res.unchanged += 1
                    continue
                if not (change.mine or m.id in mine_ids) and not scan.can_edit_others:
                    res.no_perm += 1
                    continue
                preview = preview_flag(m)
                markup = change.new_markup if change.new_markup is not None else m.reply_markup
                old_state = state_of(m.message, m.entities, m.reply_markup, preview)
                try:
                    await flood_retry(lambda: _do_edit(client, peer, m, change, markup, preview))
                except errors.MessageNotModifiedError:
                    res.unchanged += 1
                    continue
                except errors.RPCError as e:
                    name = type(e).__name__
                    log.warning("edit of %s/%s failed: %s", ch.id, m.id, name)
                    res.failed.append((m.id, name))
                    streak = streak + 1 if name == last_name else 1
                    last_name = name
                    if streak >= ABORT_AFTER:
                        res.aborted = name
                        break
                    continue
                streak, last_name = 0, None
                res.edited += 1
                kind, fid = media_info(m)
                adopt = {
                    "text": change.new_text,
                    "entities": ser_entities(change.new_entities),
                    "buttons": ser_markup(markup),
                    "link_preview": bool(preview),
                }
                if kind:
                    adopt["media_kind"], adopt["media_file_id"] = kind, fid
                buf.append(
                    {
                        "message_id": m.id,
                        "old_state": old_state,
                        "new_state": state_of(change.new_text, change.new_entities, markup, preview),
                        "adopt": adopt,
                    }
                )
                if len(buf) >= 10:
                    await flush()
                if progress:
                    await progress(res)
                await asyncio.sleep(edit_delay)
            if res.aborted:
                break
    finally:
        await flush()
    return res


async def undo_batch(
    client,
    db,
    batch_id: str,
    *,
    edit_delay: float = 1.2,
    progress: Optional[Callable[[UndoResult], Awaitable[None]]] = None,
) -> UndoResult:
    """Put every post touched by a /replace run back exactly as it was."""
    res = UndoResult()
    channels: dict = {}
    done_ids: list = []
    restored: list = []
    for c in await db.batch_changes(batch_id):
        if c.channel_id not in channels:
            channels[c.channel_id] = await db.get_channel(c.channel_id)
        ch = channels[c.channel_id]
        if ch is None:
            res.failed.append((c.message_id, "ChannelRemoved"))
            continue
        old = c.old_state or {}
        try:
            await flood_retry(
                lambda: edit_raw(
                    client,
                    peer_of(ch),
                    c.message_id,
                    text=old.get("text", ""),
                    entities=de_entities(old.get("entities")),
                    markup=build_markup(old.get("buttons")),
                    preview=old.get("preview"),
                )
            )
        except errors.MessageNotModifiedError:
            pass
        except errors.RPCError as e:
            log.warning("undo of %s/%s failed: %s", c.channel_id, c.message_id, e)
            res.failed.append((c.message_id, type(e).__name__))
            continue
        res.restored += 1
        done_ids.append(c.id)
        restored.append((c.channel_id, c.message_id, old))
        if progress:
            await progress(res)
        await asyncio.sleep(edit_delay)
    await db.finish_undo(batch_id, done_ids, restored, all_done=not res.failed)
    return res
