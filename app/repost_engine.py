"""Repost a whole channel in order.

Every post is copied to the end of the channel (silently), in the same order as the original:
  1. server-side copy (messages.forwardMessages with drop_author): text, formatting, quotes, media and
     albums are copied exactly by Telegram itself;
  2. if the channel forbids forwarding, or a copy can't be fixed up, the post is rebuilt from its parts.
Links can be swapped on the way, the new posts are registered in the database, and the old posts are removed
later in a separate step. Nothing here deletes anything unless asked to (delete_copies).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from telethon import errors, functions, types, utils

from .linkswap import compute_message_changes, url_only_markup
from .replace_engine import CHUNK, FALLBACK_EMPTY_BATCHES, fetch_messages, get_top_id, is_skippable, preview_flag
from .tgutil import classify_media, edit_raw, flood_retry, media_ref, peer_of, ser_entities, ser_markup

log = logging.getLogger(__name__)

ABORT_AFTER = 8  # consecutive posts that could not be copied -> stop
FATAL = {
    "ChatAdminRequiredError",
    "ChatWriteForbiddenError",
    "ChannelPrivateError",
    "UserBannedInChannelError",
    "ChannelInvalidError",
    "ChatRestrictedError",
}


@dataclass
class RepostOptions:
    old: Optional[str] = None
    new: Optional[str] = None
    include_typed: bool = True
    include_posts: bool = False
    last: Optional[int] = None  # trial run: only the newest N post ids

    @property
    def swaps(self) -> bool:
        return bool(self.old and self.new)


@dataclass
class Final:
    """What a copy of a post should look like."""

    text: str
    entities: list
    markup: Optional[types.ReplyInlineMarkup]
    preview: Optional[bool]
    text_changed: bool
    n_buttons: int = 0
    n_links: int = 0
    n_typed: int = 0
    dropped: int = 0  # other bots' buttons that are not copied


def final_state(src, opts: RepostOptions) -> Final:
    change = None
    if opts.swaps:
        change, _ = compute_message_changes(
            src, opts.old, opts.new, include_typed=opts.include_typed, include_posts=opts.include_posts
        )
    text = change.new_text if change else (src.message or "")
    entities = list(change.new_entities) if change else list(src.entities or [])
    # text mentions need an access hash to be re-sent; they are rare in channel posts
    entities = [e for e in entities if not isinstance(e, types.MessageEntityMentionName)]
    markup, dropped = url_only_markup(
        src.reply_markup, opts.old if opts.swaps else None, opts.new if opts.swaps else None, opts.include_posts
    )
    return Final(
        text=text,
        entities=entities,
        markup=markup,
        preview=preview_flag(src),
        text_changed=bool(change and change.entities_changed),
        n_buttons=change.n_buttons if change else 0,
        n_links=change.n_links if change else 0,
        n_typed=change.n_typed if change else 0,
        dropped=dropped,
    )


def new_messages_from(result) -> list:
    """The messages created by a send / forward request, oldest first."""
    out = []
    for u in getattr(result, "updates", None) or []:
        if isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            out.append(u.message)
    out.sort(key=lambda m: m.id)
    return out


# ------------------------------------------------------------------------------- planning
@dataclass
class RepostPlan:
    channel: Any
    first: int = 1
    last: int = 0  # newest existing message id when the plan was made
    scanned: int = 0
    units: int = 0  # posts (an album counts once)
    messages: int = 0
    albums: int = 0
    text: int = 0
    media: int = 0
    polls: int = 0
    other: int = 0
    service: int = 0
    link_posts: int = 0
    n_buttons: int = 0
    n_links: int = 0
    n_typed: int = 0
    dropped_posts: int = 0
    partial: bool = False
    error: Optional[str] = None

    def eta_seconds(self, delay: float) -> int:
        return int(self.units * (delay + 1.0))


async def find_last_id(client, peer, top: int) -> Optional[int]:
    """Newest existing message id, searching backwards from the channel counter."""
    a = top
    for _ in range(300):
        lo = max(1, a - CHUNK + 1)
        msgs = await fetch_messages(client, peer, list(range(lo, a + 1)))
        ids = [m.id for m in msgs if m is not None and not isinstance(m, types.MessageEmpty)]
        if ids:
            return max(ids)
        if lo == 1:
            return None
        a = lo - 1
    return None


async def plan_repost(
    client, ch, opts: RepostOptions, *, progress: Optional[Callable[[RepostPlan], Awaitable[None]]] = None
) -> RepostPlan:
    peer = peer_of(ch)
    plan = RepostPlan(channel=ch, partial=bool(opts.last))
    top = await get_top_id(client, peer)
    first = 1
    if opts.last:
        if top is None:
            plan.error = "I can't tell which post is the newest, so --last can't be used here."
            return plan
        newest = await find_last_id(client, peer, top)
        if newest is None:
            plan.error = "The channel has no posts."
            return plan
        first, top = max(1, newest - opts.last + 1), newest
    plan.first = first
    a, empty_run, prev_gid, max_seen = first, 0, None, 0
    while True:
        if top is not None and a > top:
            break
        hi = a + CHUNK - 1 if top is None else min(a + CHUNK - 1, top)
        found = False
        for m in await fetch_messages(client, peer, list(range(a, hi + 1))):
            if m is None or isinstance(m, types.MessageEmpty):
                continue
            found = True
            max_seen = max(max_seen, m.id)
            if isinstance(m, types.MessageService):
                plan.service += 1
                continue
            gid = getattr(m, "grouped_id", None)
            if gid is None or gid != prev_gid:
                plan.units += 1
                if gid is not None:
                    plan.albums += 1
            prev_gid = gid
            plan.messages += 1
            media = m.media
            if media is None or isinstance(media, types.MessageMediaWebPage):
                plan.text += 1
            elif isinstance(media, (types.MessageMediaPhoto, types.MessageMediaDocument)):
                plan.media += 1
            elif isinstance(media, types.MessageMediaPoll):
                plan.polls += 1
            else:
                plan.other += 1
            f = final_state(m, opts)
            if f.n_buttons or f.n_links or f.n_typed:
                plan.link_posts += 1
                plan.n_buttons += f.n_buttons
                plan.n_links += f.n_links
                plan.n_typed += f.n_typed
            if f.dropped:
                plan.dropped_posts += 1
        plan.scanned = hi
        if top is None:
            empty_run = 0 if found else empty_run + 1
            if empty_run >= FALLBACK_EMPTY_BATCHES:
                break
        if progress:
            await progress(plan)
        a = hi + 1
    plan.last = max_seen
    if not plan.units:
        plan.error = "There is nothing to copy."
    return plan


# ---------------------------------------------------------------------------------- copying
async def iter_units(client, peer, first: int, last: int) -> AsyncIterator[list]:
    """The posts between two ids, oldest first. An album is one unit (a list of messages)."""
    group: list = []
    a = first
    while a <= last:
        hi = min(a + CHUNK - 1, last)
        for m in await fetch_messages(client, peer, list(range(a, hi + 1))):
            if is_skippable(m):
                continue
            gid = getattr(m, "grouped_id", None)
            if group and gid != group[0].grouped_id:
                yield group
                group = []
            group.append(m)
            if gid is None:
                yield group
                group = []
        a = hi + 1
    if group:
        yield group


async def delete_ids(client, peer, ids: list) -> None:
    if not ids:
        return
    try:
        await flood_retry(lambda: client(functions.channels.DeleteMessagesRequest(channel=peer, id=list(ids))))
    except errors.RPCError as e:
        log.warning("could not remove messages %s: %s", ids, type(e).__name__)


async def forward_unit(client, peer, unit: list) -> list:
    ids = [m.id for m in unit]
    result = await flood_retry(
        lambda: client(
            functions.messages.ForwardMessagesRequest(from_peer=peer, id=ids, to_peer=peer, silent=True, drop_author=True)
        )
    )
    return new_messages_from(result)


async def edit_copies(client, peer, unit: list, finals: list, news: list) -> None:
    """A copy may carry the old keyboard / old links: set the final text and buttons."""
    for src, f, new in zip(unit, finals, news):
        if f.text_changed or src.reply_markup is not None:
            try:
                await flood_retry(
                    lambda: edit_raw(client, peer, new.id, text=f.text, entities=f.entities, markup=f.markup, preview=f.preview)
                )
            except errors.MessageNotModifiedError:
                pass


def _inverted(m) -> Optional[bool]:
    return True if getattr(m, "invert_media", False) else None


async def resend_unit(client, peer, unit: list, finals: list) -> list:
    """Rebuild a post from its parts (used when copying is not allowed)."""
    if len(unit) > 1:
        singles = [
            types.InputSingleMedia(media=utils.get_input_media(m.media), message=f.text, entities=f.entities or None)
            for m, f in zip(unit, finals)
        ]

        def make():
            return functions.messages.SendMultiMediaRequest(
                peer=peer, multi_media=singles, silent=True, invert_media=_inverted(unit[0])
            )

    else:
        m, f = unit[0], finals[0]
        if m.media is None or isinstance(m.media, types.MessageMediaWebPage):

            def make():
                return functions.messages.SendMessageRequest(
                    peer=peer,
                    message=f.text,
                    entities=f.entities or None,
                    reply_markup=f.markup,
                    no_webpage=True if f.preview is False else None,
                    invert_media=_inverted(m),
                    silent=True,
                )

        else:
            media = utils.get_input_media(m.media)

            def make():
                return functions.messages.SendMediaRequest(
                    peer=peer,
                    media=media,
                    message=f.text,
                    entities=f.entities or None,
                    reply_markup=f.markup,
                    invert_media=_inverted(m),
                    silent=True,
                )

    result = await flood_retry(lambda: client(make()))
    news = new_messages_from(result)
    if len(news) != len(unit):
        await delete_ids(client, peer, [n.id for n in news])
        raise RuntimeError(f"Telegram created {len(news)} messages instead of {len(unit)}")
    return news


@dataclass
class _State:
    mode: str = "forward"
    forwarded: int = 0
    rebuilt: int = 0


async def copy_unit(client, peer, unit: list, finals: list, st: _State) -> list:
    if st.mode == "forward":
        news = None
        try:
            news = await forward_unit(client, peer, unit)
            if len(news) != len(unit):
                await delete_ids(client, peer, [n.id for n in news])
                news = None
            else:
                try:
                    await edit_copies(client, peer, unit, finals, news)
                except errors.RPCError as e:
                    log.info("could not fix up a copy (%s) - rebuilding the post", type(e).__name__)
                    await delete_ids(client, peer, [n.id for n in news])
                    news = None
        except errors.ChatForwardsRestrictedError:
            st.mode = "resend"
            log.info("this channel restricts forwarding - rebuilding posts from their parts instead")
        except errors.RPCError as e:
            if type(e).__name__ in FATAL:
                raise
            log.info("copy failed (%s) - rebuilding the post", type(e).__name__)
        if news is not None:
            st.forwarded += 1
            return news
    news = await resend_unit(client, peer, unit, finals)
    st.rebuilt += 1
    return news


@dataclass
class RepostResult:
    copied_units: int = 0
    copied_messages: int = 0
    skipped_done: int = 0
    failed: list = field(default_factory=list)  # [(first old id, error name)]
    forwarded: int = 0
    rebuilt: int = 0
    aborted: Optional[str] = None
    stopped: bool = False
    first_new: Optional[int] = None
    last_new: Optional[int] = None


def _rows(unit: list, finals: list, news: list) -> list:
    rows = []
    for src, f, new in zip(unit, finals, news):
        kind = classify_media(src)
        ref = (media_ref(new) or media_ref(src)) if kind else None
        rows.append(
            {
                "old_id": src.id,
                "new_id": new.id,
                "post": {
                    "text": f.text,
                    "entities": ser_entities(f.entities),
                    "buttons": ser_markup(f.markup),
                    "media_kind": kind,
                    "media_file_id": ref,
                    "link_preview": bool(f.preview),
                },
            }
        )
    return rows


async def run_repost(
    client,
    db,
    ch,
    mig,
    user_id: int,
    *,
    delay: float = 1.2,
    progress: Optional[Callable[[RepostResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> RepostResult:
    """Copy the posts of migration `mig` (ids first_id..last_id) in order; safe to run again to continue."""
    opts = RepostOptions(
        old=mig.old_username, new=mig.new_username, include_typed=mig.include_typed, include_posts=mig.include_posts
    )
    peer = peer_of(ch)
    done = await db.migration_done_old_ids(mig.id)
    res, st, streak = RepostResult(), _State(), 0
    async for unit in iter_units(client, peer, mig.first_id, mig.last_id):
        if should_stop and should_stop():
            res.stopped = True
            break
        ids = [m.id for m in unit]
        if all(i in done for i in ids):
            res.skipped_done += 1
            continue
        finals = [final_state(m, opts) for m in unit]
        try:
            news = await copy_unit(client, peer, unit, finals, st)
        except Exception as e:  # one bad post must not stop the others
            name = type(e).__name__
            log.warning("copying %s failed: %s %s", ids, name, e)
            res.failed.append((ids[0], name))
            streak += 1
            if name in FATAL or streak >= ABORT_AFTER:
                res.aborted = name
                break
            await asyncio.sleep(delay)
            continue
        try:
            await db.record_repost(mig.id, ch.id, _rows(unit, finals, news), user_id)
        except Exception as e:  # keep the channel and the database consistent
            log.exception("could not save the copy of %s", ids)
            await delete_ids(client, peer, [n.id for n in news])
            res.failed.append((ids[0], type(e).__name__))
            res.aborted = type(e).__name__
            break
        streak = 0
        done.update(ids)
        res.copied_units += 1
        res.copied_messages += len(unit)
        res.first_new = res.first_new or news[0].id
        res.last_new = news[-1].id
        if progress:
            await progress(res)
        await asyncio.sleep(delay)
    res.forwarded, res.rebuilt = st.forwarded, st.rebuilt
    return res


# ----------------------------------------------------------------------------- deleting
@dataclass
class DeleteResult:
    deleted: int = 0
    remaining: int = 0
    blocked: bool = False  # Telegram removed nothing from a whole batch
    error: Optional[str] = None


async def delete_copies(
    client, db, ch, mig, which: str, *, delay: float = 0.5, progress: Optional[Callable[[DeleteResult], Awaitable[None]]] = None
) -> DeleteResult:
    """which='old': remove the original posts; which='new': remove the copies (undo). Oldest first, 100 per request."""
    peer = peer_of(ch)
    res = DeleteResult()
    while True:
        ids = await db.migration_pending_ids(mig.id, which, 100)
        if not ids:
            break
        try:
            await flood_retry(lambda: client(functions.channels.DeleteMessagesRequest(channel=peer, id=ids)))
        except errors.RPCError as e:
            res.error = type(e).__name__
            break
        still = await fetch_messages(client, peer, ids)
        gone = [i for i, m in zip(ids, still) if m is None or isinstance(m, types.MessageEmpty)]
        if not gone:
            res.blocked = True
            break
        await db.mark_migration_deleted(mig.id, ch.id, which, gone)
        res.deleted += len(gone)
        if progress:
            await progress(res)
        await asyncio.sleep(delay)
    counts = await db.migration_counts(mig.id)
    res.remaining = counts["old_left"] if which == "old" else counts["new_left"]
    return res
