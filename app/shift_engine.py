"""/shift: copy posts of one channel into another channel - text, formatting, media, albums, buttons and replies
- and register the copies in My posts. The source is only read, never changed or deleted.

It is the /repost copy loop pointed at two channels. Links from a shifted post to another post of the source channel
follow the content: when that post was shifted too, the link points at its copy in the destination.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from .postlinks import PostLinks
from .repost_engine import (
    CopyJob,
    DeleteResult,
    RepostOptions,
    RepostResult,
    copy_loop,
    delete_pending,
    relink_copies,
)
from .tgutil import peer_of

log = logging.getLogger(__name__)


def source_of(shift) -> SimpleNamespace:
    """The source channel as stored with the shift (it need not be registered in the bot)."""
    return SimpleNamespace(
        id=shift.src_channel_id, access_hash=shift.src_access_hash, username=shift.src_username, title=shift.src_title
    )


async def run_shift(
    client,
    db,
    shift,
    dst,
    user_id: int,
    *,
    delay: float = 1.2,
    settle_pause: float = 0.7,
    progress: Optional[Callable[[RepostResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> RepostResult:
    """Copy the posts shift.first_id..shift.last_id of the source into `dst` (a registered channel), in order.
    Safe to run again to continue: posts that already have a copy are skipped."""
    src = source_of(shift)
    idmap = dict(await db.shift_pairs(shift.id, alive_only=False))
    links = PostLinks(
        username=src.username, channel_id=src.id, ids=idmap, target_username=dst.username, target_id=dst.id
    )
    job = CopyJob(
        src_peer=peer_of(src),
        dst_peer=peer_of(dst),
        opts=RepostOptions(),
        links=links,
        first_id=shift.first_id,
        last_id=shift.last_id,
        done=await db.shift_done_ids(shift.id),
        idmap=idmap,
        record=lambda rows: db.record_shift(shift.id, dst.id, rows, user_id),
    )
    res = await copy_loop(client, job, delay=delay, progress=progress, should_stop=should_stop, settle_pause=settle_pause)
    if not res.stopped and not res.aborted:
        res.phase = "finish"
        if progress:
            await progress(res)
        try:
            pairs = await db.shift_pairs(shift.id)
            res.final = await relink_copies(
                client, db, dst, dict(pairs), [n for _, n in pairs], links=links, delay=delay,
                settle_pause=settle_pause, should_stop=should_stop,
            )
        except Exception as e:  # the copies are fine; only the links between them were not updated
            log.exception("pointing the links of a shift at the copies failed")
            res.final_error = type(e).__name__
    return res


async def delete_shift_copies(
    client,
    db,
    dst,
    shift,
    *,
    delay: float = 0.5,
    progress: Optional[Callable[[DeleteResult], Awaitable[None]]] = None,
    fallback: Optional[Callable[[list], Awaitable[Any]]] = None,
) -> DeleteResult:
    """Undo: remove the copies of this shift from the destination channel (the source is not touched)."""
    todo = await db.shift_pending_new(shift.id, 1_000_000)

    async def mark(ids: list) -> None:
        await db.mark_shift_deleted(shift.id, dst.id, ids)

    res = await delete_pending(client, peer_of(dst), todo, mark, delay=delay, progress=progress, fallback=fallback)
    res.remaining = (await db.shift_counts(shift.id))["new_left"]
    res.blocked = res.remaining > 0 and not res.error
    return res
