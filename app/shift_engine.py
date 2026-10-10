"""/shift: copy posts of one channel into another channel - text, formatting, media, albums, buttons and replies
- and register the copies in My posts. The source is only read, never changed or deleted.

It is the /repost copy loop pointed at two channels. Links from a shifted post to another post of the source channel
follow the content: when that post was shifted too, the link points at its copy in the destination.

Two optional extras are remembered with the shift (job marks), so that continuing a stopped shift keeps them:
  -c    afterwards every link to a post of the source, in ANY connected channel, is pointed at the copy
        (crosslinks.relink_after_shift; the source itself is only read);
  -all  afterwards the destination gets the source's name, description and profile photo (profile_copy.copy_profile).

A source that Telegram holds back (a copyright strike: it shows a notice instead of its posts) is copied from what
My posts saved of the posts (repost_engine: held_back_reason / stand_ins). A source that can't be read at all (a banned
or private channel) is copied from My posts alone: mark "fromdb", set when the plan was made that way.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from .crosslinks import CrossResult, relink_after_shift, shift_links
from .profile_copy import copy_profile
from .repost_engine import (
    CopyJob,
    DeleteResult,
    RepostOptions,
    RepostPlan,
    RepostResult,
    copy_loop,
    delete_pending,
    plan_from_saved,
    relink_copies,
    saved_lookup,
    saved_units,
)
from .tgutil import peer_of

log = logging.getLogger(__name__)


def source_of(shift) -> SimpleNamespace:
    """The source channel as stored with the shift (it need not be registered in the bot)."""
    return SimpleNamespace(
        id=shift.src_channel_id, access_hash=shift.src_access_hash, username=shift.src_username, title=shift.src_title
    )


async def plan_from_my_posts(db, src, first_id=None, last_id=None, unreadable=None) -> Optional[RepostPlan]:
    """The plan of a shift made from My posts alone, for a source that can't be read; None when My posts has nothing
    of that channel (in that range)."""
    plan = plan_from_saved(
        src, await db.sent_posts_from(src.id, first_id or 1), RepostOptions(), first_id=first_id, last_id=last_id,
        unreadable=unreadable,
    )
    return None if plan.error else plan


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
    links = shift_links(src, dst, idmap)
    from_db = "fromdb" in await db.marks_of(shift.id)  # the source can't be read: its posts come from My posts alone
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
        saved=saved_lookup(db, src.id),  # posts Telegram holds back (a copyright strike) are copied from My posts
        use_saved=True,
        units=(lambda: saved_units(db, src.id, shift.first_id, shift.last_id)) if from_db else None,
        from_db=from_db,
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
        marks = await db.marks_of(shift.id)
        if "profile" in marks and "profile_done" not in marks:
            await shift_profile(client, db, shift, dst, user_id, res, progress)
        if "relink" in marks and not (res.final is not None and res.final.stopped):
            res.phase = "links"
            res.cross = CrossResult()

            async def watch(_cross) -> None:
                if progress:
                    await progress(res)

            if progress:
                await progress(res)
            try:
                await relink_after_shift(
                    client, db, shift, src, dst, delay=delay, settle_pause=settle_pause, progress=watch,
                    should_stop=should_stop, result=res.cross,
                )
            except Exception as e:  # the copies are fine; only the links in the other channels were not updated
                log.exception("pointing the links of the other channels at the shifted posts failed")
                res.cross_error = type(e).__name__
    return res


async def shift_profile(client, db, shift, dst, user_id: int, res: RepostResult, progress=None) -> None:
    """-all: the destination takes over the source's name, description and photo (the outcome goes into `res`)."""
    res.phase = "profile"
    if progress:
        await progress(res)
    try:
        res.profile = await copy_profile(client, source_of(shift), dst)
        if res.profile.title:  # the destination has a new name: keep the bot's own list right
            await db.save_channel(dst.id, dst.access_hash, res.profile.title, dst.username, user_id)
        if res.profile.ok:
            await db.set_mark(shift.id, "profile_done")
    except Exception as e:  # the copies are fine; only the channel's name / description / photo were not copied
        log.exception("copying the channel profile failed")
        res.profile_error = type(e).__name__


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
