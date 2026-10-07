"""Repost a whole channel in order.

Every post is copied to the end of the channel (silently), in the same order as the original:
  1. a post that needs no change (no buttons, no link to swap) is copied by Telegram itself
     (messages.forwardMessages with drop_author): text, formatting, quotes, media and albums stay exact;
  2. a post with buttons, or with links to swap, is created again in ONE request that carries its final
     text, formatting and buttons - exactly how the bot publishes a post of its own - and is then read back
     to make sure the buttons really are there (a copy that is edited afterwards can end up without them);
  3. if the channel forbids forwarding, every post is created like in 2.
Links can be swapped on the way, the new posts are registered in the database, and the old posts are removed
later in a separate step. Nothing here deletes anything unless asked to (delete_copies).

What a plain copy would lose is put back as well:
  * a post that answers another post is posted as an answer to the COPY of that post (a reply can only be set
    when a post is created, so such a post is always created again instead of copied);
  * a link to another post of the channel (t.me/name/115) is pointed at the copy of that post - at once when the
    copy already exists, afterwards (finalize_repost) for posts that are copied later;
  * a post that was pinned has its copy pinned at the end.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from telethon import errors, functions, types, utils

from .buttons_sync import ButtonsNotShown, ensure_markup, link_urls
from .linkswap import compute_message_changes, url_only_markup
from .postlinks import PostLinks, relink_message
from .replace_engine import CHUNK, FALLBACK_EMPTY_BATCHES, fetch_messages, get_top_id, is_skippable, preview_flag
from .tgutil import (
    build_markup,
    classify_media,
    de_entities,
    edit_raw,
    flood_retry,
    media_ref,
    peer_of,
    ser_entities,
    ser_markup,
)

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
    n_post_links: int = 0  # links to other posts of the same channel (whatever their new id will be)
    n_relinked: int = 0  # of those, how many were already pointed at a copy


def final_state(src, opts: RepostOptions, links: Optional[PostLinks] = None) -> Final:
    """What the copy of `src` should look like. `links` (optional) points links to other posts of the channel at
    the copies that exist so far (links.ids: old message id -> new one)."""
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
    n_post_links = n_relinked = 0
    text_changed = bool(change and change.entities_changed)
    if links is not None:
        n_post_links = links.count(text, entities, markup)
        if n_post_links and links.ids:
            text, entities, n_typed, n_hyper = links.remap_text(text, entities)
            markup, n_buttons = links.remap_markup(markup)
            n_relinked = n_typed + n_hyper + n_buttons
            text_changed = text_changed or bool(n_typed or n_hyper)
    # Send exactly what is saved: go through the stored form that "My posts" also uses to build its edits.
    markup = build_markup(ser_markup(markup)) if markup is not None else None
    entities = de_entities(ser_entities(entities))
    return Final(
        text=text,
        entities=entities,
        markup=markup,
        preview=preview_flag(src),
        text_changed=text_changed,
        n_buttons=change.n_buttons if change else 0,
        n_links=change.n_links if change else 0,
        n_typed=change.n_typed if change else 0,
        dropped=dropped,
        n_post_links=n_post_links,
        n_relinked=n_relinked,
    )


# ---------------------------------------------------------------------------- replies and pins
class ReplyParentMissing(RuntimeError):
    """A post answers another post whose copy does not exist (yet): copying it now would lose the answer."""


def reply_parent(msg) -> Optional[int]:
    """The message id a post answers (in the same channel), or None."""
    h = getattr(msg, "reply_to", None)
    if h is None or getattr(h, "reply_to_peer_id", None) is not None or getattr(h, "forum_topic", None):
        return None
    return getattr(h, "reply_to_msg_id", None) or None


def new_parent(msg, idmap: dict, seen: set) -> Optional[int]:
    """The copy of the post `msg` answers. None when it answers nothing, or the post it answered is gone / outside
    the range being copied. Raises ReplyParentMissing when that post was read in this run but has no copy."""
    pid = reply_parent(msg)
    if pid is None:
        return None
    if pid in idmap:
        return idmap[pid]
    if pid in seen:
        raise ReplyParentMissing(f"post {msg.id} answers post {pid}, which has not been copied")
    return None


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
    post_link_posts: int = 0  # posts with a link to another post of this channel
    post_links: int = 0
    pinned: int = 0  # pinned posts (their copies get pinned)
    replies: int = 0  # posts that answer another post
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
    client,
    ch,
    opts: RepostOptions,
    *,
    progress: Optional[Callable[[RepostPlan], Awaitable[None]]] = None,
    first_id: Optional[int] = None,
    last_id: Optional[int] = None,
) -> RepostPlan:
    """Read the channel and count what a copy would do. first_id / last_id limit the range (used by /shift)."""
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
    if first_id:
        first = max(first, first_id)
        plan.partial = True
    if last_id:
        top = last_id if top is None else min(top, last_id)
        plan.partial = True
    plan.first = first
    links = PostLinks.for_channel(ch)
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
            f = final_state(m, opts, links)
            if f.n_buttons or f.n_links or f.n_typed:
                plan.link_posts += 1
                plan.n_buttons += f.n_buttons
                plan.n_links += f.n_links
                plan.n_typed += f.n_typed
            if f.dropped:
                plan.dropped_posts += 1
            if f.n_post_links:
                plan.post_link_posts += 1
                plan.post_links += f.n_post_links
            if getattr(m, "pinned", False):
                plan.pinned += 1
            if reply_parent(m) is not None:
                plan.replies += 1
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


async def forward_unit(client, src_peer, unit: list, dst_peer=None) -> list:
    """Let Telegram copy the posts (no "forwarded from" header). `dst_peer` defaults to the same channel."""
    ids = [m.id for m in unit]
    to_peer = dst_peer if dst_peer is not None else src_peer
    result = await flood_retry(
        lambda: client(
            functions.messages.ForwardMessagesRequest(
                from_peer=src_peer, id=ids, to_peer=to_peer, silent=True, drop_author=True
            )
        )
    )
    return new_messages_from(result)


def _inverted(m) -> Optional[bool]:
    return True if getattr(m, "invert_media", False) else None


def reply_header(reply_to: Optional[int]):
    """The `reply_to` argument of the send requests: answer message `reply_to` of the same channel."""
    return types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None


async def resend_unit(client, peer, unit: list, finals: list, reply_to: Optional[int] = None) -> list:
    """Rebuild a post from its parts (used when copying is not allowed, or the copy must differ from the original).
    `reply_to`: message id (in the channel `peer`) the new post answers."""
    reply = reply_header(reply_to)
    if len(unit) > 1:
        singles = [
            types.InputSingleMedia(media=utils.get_input_media(m.media), message=f.text, entities=f.entities or None)
            for m, f in zip(unit, finals)
        ]

        def make():
            return functions.messages.SendMultiMediaRequest(
                peer=peer, multi_media=singles, silent=True, invert_media=_inverted(unit[0]), reply_to=reply
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
                    reply_to=reply,
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
                    reply_to=reply,
                )

    result = await flood_retry(lambda: client(make()))
    news = new_messages_from(result)
    if len(news) != len(unit):
        await delete_ids(client, peer, [n.id for n in news])
        raise RuntimeError(f"Telegram created {len(news)} messages instead of {len(unit)}")
    return news


@dataclass
class _State:
    mode: str = "forward"  # becomes "resend" when the channel forbids forwarding
    forwarded: int = 0
    rebuilt: int = 0
    repaired: int = 0  # new posts whose buttons had to be set again after posting
    replies: int = 0  # new posts created as an answer to the copy of the post the original answered
    reply_lost: int = 0  # answers that could only be copied without the reply


def is_pure(src, f: Final, reply_to: Optional[int] = None) -> bool:
    """True when a server-side copy is already the final post: nothing to swap, no keyboard to rebuild and no
    reply to set (a reply can only be given when a post is created)."""
    return not f.text_changed and src.reply_markup is None and not reply_to


async def copy_unit(
    client, peer, unit: list, finals: list, st: _State, reply_to: Optional[int] = None, dst_peer=None
) -> list:
    """Create the copy of one post (or album) at the end of `dst_peer` (default: the same channel `peer`)."""
    dst = dst_peer if dst_peer is not None else peer
    pure = all(is_pure(m, f, reply_to) for m, f in zip(unit, finals))
    if st.mode == "forward" and pure:
        try:
            news = await forward_unit(client, peer, unit, dst)
            if len(news) == len(unit):
                st.forwarded += 1
                return news
            await delete_ids(client, dst, [n.id for n in news])
        except errors.ChatForwardsRestrictedError:
            st.mode = "resend"
            log.info("this channel restricts forwarding - rebuilding posts from their parts instead")
        except errors.RPCError as e:
            if type(e).__name__ in FATAL:
                raise
            log.info("copy failed (%s) - rebuilding the post", type(e).__name__)
    try:
        news = await resend_unit(client, dst, unit, finals, reply_to)
    except (TypeError, ValueError, errors.RPCError) as e:
        # A kind of media a post cannot be rebuilt from (or a reply Telegram refuses). If only the reply made a
        # rebuild necessary, a plain copy by Telegram still works - without the reply.
        if isinstance(e, errors.RPCError) and type(e).__name__ in FATAL:
            raise
        if reply_to and st.mode == "forward" and all(is_pure(m, f) for m, f in zip(unit, finals)):
            news = await forward_unit(client, peer, unit, dst)
            if len(news) != len(unit):
                await delete_ids(client, dst, [n.id for n in news])
                raise
            st.reply_lost += 1
            st.forwarded += 1
            return news
        raise
    st.rebuilt += 1
    if reply_to:
        st.replies += 1
    return news


async def settle_buttons(client, peer, finals: list, news: list, st: _State, *, pause: float) -> None:
    """Read the new posts back: each must show the link buttons it should. Repairs it, or removes the copies."""
    for f, new in zip(finals, news):
        if not link_urls(f.markup):
            continue
        try:
            how = await ensure_markup(client, peer, new.id, f.markup, pause=pause)
        except errors.RPCError as e:
            await delete_ids(client, peer, [n.id for n in news])
            if type(e).__name__ in FATAL:
                raise
            raise ButtonsNotShown(f"could not set the buttons ({type(e).__name__})") from e
        except (ButtonsNotShown, LookupError) as e:
            await delete_ids(client, peer, [n.id for n in news])
            raise ButtonsNotShown(str(e)) from e
        if how != "fine":
            st.repaired += 1


@dataclass
class FinalizeResult:
    """The finishing touches after the copies exist: links to other posts, pins."""

    checked: int = 0  # copies read back
    relinked: int = 0  # copies whose links to other posts now point at the new copies
    links: int = 0  # links changed in total
    missing: int = 0  # copies that do not exist any more
    failed: list = field(default_factory=list)  # [(message id, error name)]
    pin_wanted: int = 0  # copies of pinned posts that still had to be pinned
    pinned: int = 0
    pin_error: Optional[str] = None
    aborted: Optional[str] = None
    stopped: bool = False


@dataclass
class RepostResult:
    copied_units: int = 0
    copied_messages: int = 0
    skipped_done: int = 0
    failed: list = field(default_factory=list)  # [(first old id, error name)]
    forwarded: int = 0
    rebuilt: int = 0
    repaired: int = 0
    replies: int = 0  # copies created as an answer to the copy of the post the original answered
    reply_lost: int = 0  # answers that could only be copied without the reply
    reply_dropped: int = 0  # answers to a post that no longer exists (nothing to answer)
    aborted: Optional[str] = None
    stopped: bool = False
    first_new: Optional[int] = None
    last_new: Optional[int] = None
    phase: str = "copy"  # copy | finish
    final: Optional[FinalizeResult] = None
    final_error: Optional[str] = None


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


@dataclass
class CopyJob:
    """Everything the copy loop needs to know; /repost and /shift each fill it in their own way."""

    src_peer: Any
    dst_peer: Any  # the same as src_peer for /repost
    opts: RepostOptions
    links: PostLinks  # links.ids is `idmap`: old message id -> id of its copy, grows as we go
    first_id: int
    last_id: int
    done: set  # old ids that already have a copy
    idmap: dict
    record: Callable[[list], Awaitable[None]]  # saves [{old_id, new_id, post}] (and registers the posts)


async def copy_loop(
    client,
    job: CopyJob,
    *,
    delay: float = 1.2,
    progress: Optional[Callable[[RepostResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    settle_pause: float = 0.7,
) -> RepostResult:
    """Copy the posts first_id..last_id of `job.src_peer` to the end of `job.dst_peer`, in order.
    Safe to run again to continue: posts in `job.done` are skipped."""
    src, dst = job.src_peer, job.dst_peer
    seen: set = set()  # old ids read in this run (a reply to one of them needs its copy)
    res, st, streak = RepostResult(), _State(), 0
    async for unit in iter_units(client, src, job.first_id, job.last_id):
        if should_stop and should_stop():
            res.stopped = True
            break
        ids = [m.id for m in unit]
        seen.update(ids)
        if all(i in job.done for i in ids):
            res.skipped_done += 1
            continue
        finals = [final_state(m, job.opts, job.links) for m in unit]
        try:
            reply_to = new_parent(unit[0], job.idmap, seen)
            news = await copy_unit(client, src, unit, finals, st, reply_to, dst)
            await settle_buttons(client, dst, finals, news, st, pause=settle_pause)
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
            await job.record(_rows(unit, finals, news))
        except Exception as e:  # keep the channel and the database consistent
            log.exception("could not save the copy of %s", ids)
            await delete_ids(client, dst, [n.id for n in news])
            res.failed.append((ids[0], type(e).__name__))
            res.aborted = type(e).__name__
            break
        streak = 0
        job.done.update(ids)
        for old, new in zip(unit, news):
            job.idmap[old.id] = new.id
        if reply_parent(unit[0]) is not None and reply_to is None:
            res.reply_dropped += 1
        res.copied_units += 1
        res.copied_messages += len(unit)
        res.first_new = res.first_new or news[0].id
        res.last_new = news[-1].id
        if progress:
            await progress(res)
        await asyncio.sleep(delay)
    res.forwarded, res.rebuilt, res.repaired = st.forwarded, st.rebuilt, st.repaired
    res.replies, res.reply_lost = st.replies, st.reply_lost
    return res


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
    settle_pause: float = 0.7,
) -> RepostResult:
    """Copy the posts of migration `mig` (ids first_id..last_id) in order; safe to run again to continue.

    When every post is copied (not stopped, no fatal error) the finishing touches follow: links to posts that were
    copied later are pointed at their copies, and the copies of pinned posts are pinned."""
    opts = RepostOptions(
        old=mig.old_username, new=mig.new_username, include_typed=mig.include_typed, include_posts=mig.include_posts
    )
    peer = peer_of(ch)
    idmap = dict(await db.migration_pairs(mid=mig.id, alive_only=False))
    job = CopyJob(
        src_peer=peer,
        dst_peer=peer,
        opts=opts,
        links=PostLinks.for_channel(ch, idmap),
        first_id=mig.first_id,
        last_id=mig.last_id,
        done=await db.migration_done_old_ids(mig.id),
        idmap=idmap,
        record=lambda rows: db.record_repost(mig.id, ch.id, rows, user_id),
    )
    res = await copy_loop(client, job, delay=delay, progress=progress, should_stop=should_stop, settle_pause=settle_pause)
    if not res.stopped and not res.aborted:
        res.phase = "finish"
        if progress:
            await progress(res)
        try:
            res.final = await finalize_repost(
                client, db, ch, mig, delay=delay, settle_pause=settle_pause, should_stop=should_stop
            )
        except Exception as e:  # the copies are fine; only the finishing touches failed
            log.exception("finishing the repost (links, pins) failed")
            res.final_error = type(e).__name__
    return res


# --------------------------------------------------------------------- links to copies, pins
async def relink_copies(
    client,
    db,
    ch,
    ids: dict,
    targets: list,
    *,
    delay: float = 1.2,
    settle_pause: float = 0.7,
    dry_run: bool = False,
    progress: Optional[Callable[[FinalizeResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    links: Optional[PostLinks] = None,
) -> FinalizeResult:
    """Point every link to a post of this channel at the copy of that post (`ids`: old message id -> new one).

    `targets` are the message ids of `ch` to look at (the copies). Idempotent: a link that already points at a copy
    is left alone, because only OLD ids are replaced. With dry_run nothing is edited, the result only counts.
    `links` (optional) says which links to follow; the default is "links to posts of `ch` itself" (/repost)."""
    res = FinalizeResult()
    peer = peer_of(ch)
    links = links if links is not None else PostLinks.for_channel(ch, ids)
    targets = sorted(set(targets))
    streak, last_name = 0, None
    for i in range(0, len(targets), CHUNK):
        chunk = targets[i : i + CHUNK]
        for mid, m in zip(chunk, await fetch_messages(client, peer, chunk)):
            if is_skippable(m):
                res.missing += 1
                continue
            res.checked += 1
            rl = relink_message(m, links)
            if rl is None:
                continue
            if dry_run:
                res.relinked += 1
                res.links += rl.links
                continue
            if should_stop and should_stop():
                res.stopped = True
                return res
            try:
                await flood_retry(
                    lambda: edit_raw(
                        client, peer, m.id, text=rl.text, entities=rl.entities, markup=rl.markup, preview=preview_flag(m)
                    )
                )
                if link_urls(rl.markup):
                    await ensure_markup(client, peer, m.id, rl.markup, pause=settle_pause)
            except errors.MessageNotModifiedError:
                continue
            except Exception as e:  # one bad post must not stop the others
                name = type(e).__name__
                log.warning("could not update the links of %s: %s %s", m.id, name, e)
                res.failed.append((m.id, name))
                streak = streak + 1 if name == last_name else 1
                last_name = name
                if name in FATAL or streak >= ABORT_AFTER:
                    res.aborted = name
                    return res
                continue
            streak, last_name = 0, None
            res.relinked += 1
            res.links += rl.links
            try:
                await db.update_post_by_message(
                    ch.id, m.id, text=rl.text, entities=ser_entities(rl.entities), buttons=ser_markup(rl.markup)
                )
            except Exception:  # the channel is right; the saved copy will be corrected by the next sync
                log.warning("could not save the new links of %s", m.id, exc_info=True)
            if progress:
                await progress(res)
            await asyncio.sleep(delay)
    return res


async def pin_copies(client, ch, pairs: list, res: FinalizeResult, *, delay: float = 1.2) -> None:
    """Pin the copy of every post that is pinned among the originals (oldest first, so the newest pin is on top).

    The "pinned a message" notices Telegram adds to the channel are deleted again right away."""
    peer = peer_of(ch)
    pairs = sorted(pairs)
    todo: list = []
    for i in range(0, len(pairs), CHUNK):
        chunk = pairs[i : i + CHUNK]
        olds = await fetch_messages(client, peer, [o for o, _ in chunk])
        news = await fetch_messages(client, peer, [n for _, n in chunk])
        for (_, new_id), src, cur in zip(chunk, olds, news):
            if is_skippable(src) or is_skippable(cur):
                continue
            if getattr(src, "pinned", False) and not getattr(cur, "pinned", False):
                todo.append(new_id)
    res.pin_wanted = len(todo)
    for new_id in todo:
        try:
            out = await flood_retry(
                lambda: client(functions.messages.UpdatePinnedMessageRequest(peer=peer, id=new_id, silent=True))
            )
        except errors.MessageNotModifiedError:
            continue  # already pinned
        except errors.RPCError as e:
            res.pin_error = type(e).__name__
            return
        res.pinned += 1
        await delete_ids(client, peer, [m.id for m in new_messages_from(out) if isinstance(m, types.MessageService)])
        await asyncio.sleep(delay)


async def finalize_repost(
    client,
    db,
    ch,
    mig,
    *,
    delay: float = 1.2,
    settle_pause: float = 0.7,
    progress: Optional[Callable[[FinalizeResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> FinalizeResult:
    """After the copies exist: links that point at old posts get the copies' ids, copies of pinned posts get pinned."""
    pairs = sorted(await db.migration_pairs(mid=mig.id))
    res = await relink_copies(
        client, db, ch, dict(pairs), [n for _, n in pairs], delay=delay, settle_pause=settle_pause,
        progress=progress, should_stop=should_stop,
    )
    if not res.stopped and not res.aborted:
        await pin_copies(client, ch, pairs, res, delay=delay)
    return res


# ----------------------------------------------------------------------------- deleting
@dataclass
class DeleteResult:
    deleted: int = 0
    by_userbot: int = 0  # of those, how many only the userbot could delete
    remaining: int = 0
    blocked: bool = False  # some messages are still there although Telegram did not report an error
    error: Optional[str] = None  # the bot's Telegram error that stopped the run
    fallback_error: Optional[str] = None  # why the userbot could not help
    tried_userbot: bool = False


async def gone_ids(client, peer, ids: list) -> set:
    """Which of `ids` no longer exist in the channel."""
    msgs = await fetch_messages(client, peer, ids)
    return {i for i, m in zip(ids, msgs) if m is None or isinstance(m, types.MessageEmpty)}


@dataclass
class BatchOutcome:
    gone: set = field(default_factory=set)  # message ids that no longer exist afterwards
    bot_error: Optional[str] = None
    by_userbot: int = 0
    fallback_error: Optional[str] = None
    tried_userbot: bool = False


async def delete_batch(client, peer, ids: list, fallback: Optional[Callable[[list], Awaitable[Any]]] = None) -> BatchOutcome:
    """Delete up to 100 messages: the bot tries first, the result is checked by reading the messages back, and
    whatever is still there goes to `fallback(ids)` (the userbot), followed by a second check."""
    out = BatchOutcome()
    try:
        await flood_retry(lambda: client(functions.channels.DeleteMessagesRequest(channel=peer, id=list(ids))))
    except errors.RPCError as e:
        out.bot_error = type(e).__name__
    out.gone = await gone_ids(client, peer, ids)
    left = [x for x in ids if x not in out.gone]
    if left and fallback is not None:
        out.tried_userbot = True
        try:
            await fallback(left)
        except Exception as e:  # a userbot problem must not hide what the bot already did
            log.warning("the userbot could not delete %s message(s): %s", len(left), e)
            out.fallback_error = str(e) or type(e).__name__
        again = await gone_ids(client, peer, left)
        out.by_userbot = len(again)
        out.gone |= again
    return out


async def delete_pending(
    client,
    peer,
    todo: list,
    mark: Callable[[list], Awaitable[None]],
    *,
    delay: float = 0.5,
    progress: Optional[Callable[[DeleteResult], Awaitable[None]]] = None,
    fallback: Optional[Callable[[list], Awaitable[Any]]] = None,
) -> DeleteResult:
    """Delete the messages `todo` (oldest first, 100 per request). `mark(ids)` records the ones that are gone.

    The bot tries first and the result is always checked by reading the messages back. Whatever the bot could not
    delete (Telegram may refuse old posts) goes to `fallback(ids)` - the userbot - if there is one, and is checked
    again. A batch the bot cannot delete does not stop the run: newer posts may still be deletable."""
    res = DeleteResult()
    use_fallback = fallback
    for i in range(0, len(todo), 100):
        ids = todo[i : i + 100]
        out = await delete_batch(client, peer, ids, use_fallback)
        res.tried_userbot = res.tried_userbot or out.tried_userbot
        res.by_userbot += out.by_userbot
        if out.fallback_error:
            res.fallback_error = out.fallback_error
            use_fallback = None  # it will not work for the next batches either
        left = [x for x in ids if x not in out.gone]
        if out.gone:
            await mark(sorted(out.gone))
            res.deleted += len(out.gone)
        if left and out.bot_error:
            res.error = res.error or out.bot_error  # remembered for the report
            if out.bot_error in FATAL and use_fallback is None:
                break  # e.g. no "Delete messages" right: the next batch would fail the same way
        if progress:
            await progress(res)
        await asyncio.sleep(delay)
    return res


async def delete_copies(
    client,
    db,
    ch,
    mig,
    which: str,
    *,
    delay: float = 0.5,
    progress: Optional[Callable[[DeleteResult], Awaitable[None]]] = None,
    fallback: Optional[Callable[[list], Awaitable[Any]]] = None,
) -> DeleteResult:
    """which='old': remove the original posts; which='new': remove the copies (undo). Oldest first, 100 per request."""
    todo = await db.migration_pending_ids(mig.id, which, 1_000_000)

    async def mark(ids: list) -> None:
        await db.mark_migration_deleted(mig.id, ch.id, which, ids)

    res = await delete_pending(client, peer_of(ch), todo, mark, delay=delay, progress=progress, fallback=fallback)
    counts = await db.migration_counts(mig.id)
    res.remaining = counts["old_left"] if which == "old" else counts["new_left"]
    res.blocked = res.remaining > 0 and not res.error
    return res
