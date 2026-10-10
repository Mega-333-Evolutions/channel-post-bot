"""Links to posts that moved: pointed at their new place in EVERY connected channel.

/repost gives every post of a channel a new message id, and /shift -c puts the posts into another channel. A link to
the old post - https://t.me/name/52, https://t.me/c/123/52, a hyperlink behind some text, a button - that sits in ANY
connected channel would lead nowhere once the old post is gone. relink_channels reads the connected channels, finds
those links and points them at the equivalent new post. Only the digits of the id change (for /shift also the channel
part of the link); the text around a link, the formatting and all other buttons stay exactly as they are.

Rules
  * only a link whose old post HAS a copy is changed: a link to a post that was not copied stays as it is;
  * running it again changes nothing more - a link that already points at a copy is not an old link any more;
  * a post is read again right before it is edited, so something an admin changed meanwhile is never overwritten;
  * a post of somebody else is edited only if the bot has the "Edit messages of others" right; otherwise it is only
    counted, so the report can say so;
  * what Telegram holds back (a copyright notice instead of the post) and a channel Telegram restricts are not edited;
  * nothing is deleted anywhere.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from telethon import errors, types

from .buttons_sync import ensure_markup, link_urls
from .postlinks import PostLinks, relink_message
from .replace_engine import CHUNK, fetch_messages, is_skippable, preview_flag
from .restrictions import message_restriction, probe_channel
from .tgutil import FATAL, edit_raw, esc, flood_retry, get_rights, peer_of, ser_entities, ser_markup

log = logging.getLogger(__name__)

PAUSE = 0.25  # between two reads while a channel is scanned
ABORT_AFTER = 5  # the same refusal this many times in a row: leave the channel (the others are still done)


@dataclass
class ChannelLinks:
    """What was found and done in one channel."""

    channel: Any
    scanned: int = 0  # message ids read so far
    top: int = 0  # the last id that is read
    checked: int = 0  # posts looked at
    found: int = 0  # posts with a link to change
    relinked: int = 0  # posts whose links were changed
    links: int = 0  # links changed in total
    not_allowed: int = 0  # posts with a link to change that the bot may not edit (somebody else's, no right)
    held_back: int = 0  # posts Telegram holds back (copyright ...): left as they are
    gone: int = 0  # posts that were deleted while we worked
    failed: list = field(default_factory=list)  # [(message id, error name)]
    skipped: Optional[str] = None  # why the channel was not looked at
    aborted: Optional[str] = None  # the refusal that made us give up on this channel


@dataclass
class CrossResult:
    """What a run did in all the channels (also handed to `progress` while it works)."""

    channels: list = field(default_factory=list)  # [ChannelLinks], in the order they were done
    total: int = 0  # channels to look at
    index: int = 0  # the number of the channel being worked on (1 = the first)
    current: Optional[ChannelLinks] = None
    phase: str = ""  # scan | edit
    stopped: bool = False

    @property
    def relinked(self) -> int:
        return sum(c.relinked for c in self.channels)

    @property
    def links(self) -> int:
        return sum(c.links for c in self.channels)

    @property
    def failed(self) -> list:
        return [(c.channel, mid, name) for c in self.channels for mid, name in c.failed]

    @property
    def not_allowed(self) -> int:
        return sum(c.not_allowed for c in self.channels)

    @property
    def changed_channels(self) -> list:
        return [c for c in self.channels if c.relinked]

    @property
    def skipped(self) -> list:
        return [c for c in self.channels if c.skipped]

    @property
    def clean(self) -> bool:
        """Everything that had to be done was done."""
        return not self.stopped and not self.failed and not self.not_allowed and not any(c.aborted for c in self.channels)


async def _edit(client, peer, m, rl) -> None:
    if rl.text_changed:
        await edit_raw(client, peer, m.id, text=rl.text, entities=rl.entities, markup=rl.markup, preview=preview_flag(m))
    else:  # only buttons changed: the text is not sent again
        await edit_raw(client, peer, m.id, markup=rl.markup)


async def _scan(
    client, peer, top: int, links: PostLinks, leave: set, mine: set, can_edit_others: bool, res: ChannelLinks, *,
    pause: float, tick: Callable[[], Awaitable[None]], should_stop: Optional[Callable[[], bool]],
) -> list:
    """Read ids 1..top; the ids of the posts that have links to change (and may be changed) come back, oldest first."""
    todo: list = []
    a = 1
    while a <= top:
        if should_stop and should_stop():
            break
        hi = min(a + CHUNK - 1, top)
        for m in await fetch_messages(client, peer, list(range(a, hi + 1))):
            if m is None or isinstance(m, (types.MessageEmpty, types.MessageService)):
                continue
            res.checked += 1
            if m.id in leave:
                continue
            if message_restriction(m):
                res.held_back += 1
                continue
            if relink_message(m, links) is None:
                continue
            res.found += 1
            if not (getattr(m, "out", False) or m.id in mine) and not can_edit_others:
                res.not_allowed += 1
                continue
            todo.append(m.id)
        res.scanned = hi
        a = hi + 1
        await tick()
        if pause and a <= top:
            await asyncio.sleep(pause)
    return todo


async def _apply(
    client, db, ch, peer, ids: list, links: PostLinks, res: ChannelLinks, *,
    delay: float, settle_pause: float, tick: Callable[[], Awaitable[None]], should_stop: Optional[Callable[[], bool]],
) -> bool:
    """Edit the posts in `ids`, each one read again first. False when the run was stopped."""
    streak, last_name = 0, None
    for mid in ids:
        if should_stop and should_stop():
            return False
        (m,) = await fetch_messages(client, peer, [mid])
        if is_skippable(m):
            res.gone += 1
            continue
        if message_restriction(m):
            res.held_back += 1
            continue
        rl = relink_message(m, links)
        if rl is None:  # somebody fixed it meanwhile
            continue
        try:
            await flood_retry(lambda: _edit(client, peer, m, rl))
            if link_urls(rl.markup):
                await ensure_markup(client, peer, m.id, rl.markup, pause=settle_pause)
        except errors.MessageNotModifiedError:
            continue
        except Exception as e:  # one bad post must not stop the others
            name = type(e).__name__
            log.warning("could not update the links of %s/%s: %s %s", ch.id, m.id, name, e)
            res.failed.append((m.id, name))
            streak = streak + 1 if name == last_name else 1
            last_name = name
            if name in FATAL or streak >= ABORT_AFTER:
                res.aborted = name
                return True
            continue
        streak, last_name = 0, None
        res.relinked += 1
        res.links += rl.links
        fields: dict = {}
        if rl.text_changed:
            fields.update(text=rl.text, entities=ser_entities(rl.entities))
        if rl.markup_changed:
            fields["buttons"] = ser_markup(rl.markup)
        try:
            await db.update_post_by_message(ch.id, m.id, **fields)
        except Exception:  # the channel is right; the next sync corrects the saved copy
            log.warning("could not save the new links of %s/%s", ch.id, m.id, exc_info=True)
        await tick()
        await asyncio.sleep(delay)
    return True


async def relink_channels(
    client,
    db,
    links: PostLinks,
    channels: list,
    *,
    leave: Optional[dict] = None,
    delay: float = 1.2,
    settle_pause: float = 0.7,
    pause: float = PAUSE,
    progress: Optional[Callable[[CrossResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    result: Optional[CrossResult] = None,
) -> CrossResult:
    """Point every link that `links` knows (old post -> its new place) at the new place, in each of `channels`.

    `leave` ({channel id: message ids}) are posts that must not be touched, such as the originals of a repost.
    `result` may be given to watch a run from outside: it is filled in while the run goes on."""
    res = result if result is not None else CrossResult()
    res.total = len(channels)
    leave = leave or {}
    if not links.ids:
        return res

    async def tick() -> None:
        if progress:
            await progress(res)

    for n, ch in enumerate(channels, 1):
        if should_stop and should_stop():
            res.stopped = True
            break
        one = ChannelLinks(channel=ch)
        res.channels.append(one)
        res.index, res.current, res.phase = n, one, "scan"
        await tick()
        peer = peer_of(ch)
        rights = await get_rights(client, ch)
        if rights is not None and not rights.admin:
            one.skipped = "the bot is not an admin there"
            continue
        if rights is not None and not (rights.post or rights.edit):
            one.skipped = "the bot has no right to post or edit there"
            continue
        can_others = True if rights is None else bool(rights.edit)
        top, why, err = await probe_channel(client, peer)
        if err:
            one.skipped = err
            continue
        if why:
            one.skipped = f"Telegram restricts this channel ({why})"
            continue
        one.top = top or 0
        try:
            todo = await _scan(
                client, peer, one.top, links, set(leave.get(ch.id, ())), await db.mine_message_ids(ch.id), can_others,
                one, pause=pause, tick=tick, should_stop=should_stop,
            )
            if should_stop and should_stop():
                res.stopped = True
                break
            res.phase = "edit"
            await tick()
            if not await _apply(
                client, db, ch, peer, todo, links, one, delay=delay, settle_pause=settle_pause, tick=tick,
                should_stop=should_stop,
            ):
                res.stopped = True
                break
        except errors.RPCError as e:  # the channel can't be read (any more): the others still can
            log.warning("could not read %s while pointing links at the new posts: %s", ch.id, type(e).__name__)
            one.aborted = type(e).__name__
    res.phase, res.current = "", None
    return res


# ------------------------------------------------------------------------------ the three ways to use it
async def relink_after_repost(
    client, db, ch, mig, *, delay: float = 1.2, settle_pause: float = 0.7, pause: float = PAUSE,
    progress=None, should_stop=None, result: Optional[CrossResult] = None,
) -> CrossResult:
    """After /repost: every link to an old post of `ch`, in any connected channel, points at the copy of that post.
    The originals themselves are not touched (they are deleted later, or kept if the repost is undone)."""
    res = result if result is not None else CrossResult()
    ids = dict(await db.migration_pairs(mid=mig.id))
    if not ids:
        return res
    await db.set_mark(mig.id, "links")  # from now on an undo has to point the links back
    return await relink_channels(
        client, db, PostLinks.for_channel(ch, ids), await db.list_channels(), leave={ch.id: set(ids)}, delay=delay,
        settle_pause=settle_pause, pause=pause, progress=progress, should_stop=should_stop, result=res,
    )


async def restore_after_undo(
    client, db, ch, mig, *, delay: float = 1.2, settle_pause: float = 0.7, pause: float = PAUSE,
    progress=None, should_stop=None, result: Optional[CrossResult] = None,
) -> CrossResult:
    """Before the copies of a repost are removed: links that were pointed at them go back to the original posts."""
    res = result if result is not None else CrossResult()
    if not await db.has_mark(mig.id, "links"):
        return res  # no link anywhere was changed for this repost
    back = {new: old for old, new in await db.migration_pairs(mid=mig.id)}
    return await relink_channels(
        client, db, PostLinks.for_channel(ch, back), await db.list_channels(), leave={ch.id: set(back)}, delay=delay,
        settle_pause=settle_pause, pause=pause, progress=progress, should_stop=should_stop, result=res,
    )


def shift_links(src, dst, ids: dict) -> PostLinks:
    """Links to the posts of the source channel -> the equivalent posts of the destination (`ids`: source id -> copy)."""
    return PostLinks(
        username=src.username, channel_id=src.id, ids=ids, target_username=dst.username, target_id=dst.id
    )


async def relink_after_shift(
    client, db, shift, src, dst, *, delay: float = 1.2, settle_pause: float = 0.7, pause: float = PAUSE,
    progress=None, should_stop=None, result: Optional[CrossResult] = None,
) -> CrossResult:
    """After /shift -c: a link to a post of the source channel, in any connected channel (the source itself is only
    read, never changed), points at the copy of that post in the destination."""
    res = result if result is not None else CrossResult()
    ids = dict(await db.shift_pairs(shift.id))
    if not ids:
        return res
    await db.set_mark(shift.id, "links")
    chans = [c for c in await db.list_channels() if c.id != src.id]
    return await relink_channels(
        client, db, shift_links(src, dst, ids), chans, delay=delay, settle_pause=settle_pause, pause=pause,
        progress=progress, should_stop=should_stop, result=res,
    )


async def restore_after_shift_undo(
    client, db, shift, src, dst, *, delay: float = 1.2, settle_pause: float = 0.7, pause: float = PAUSE,
    progress=None, should_stop=None, result: Optional[CrossResult] = None,
) -> CrossResult:
    """Before the copies of a shift are removed: links that were pointed at them go back to the source posts."""
    res = result if result is not None else CrossResult()
    if not await db.has_mark(shift.id, "links"):
        return res
    back = {new: old for old, new in await db.shift_pairs(shift.id)}
    links = PostLinks(
        username=dst.username, channel_id=dst.id, ids=back, target_username=src.username, target_id=src.id
    )
    chans = [c for c in await db.list_channels() if c.id != src.id]
    return await relink_channels(
        client, db, links, chans, leave={dst.id: set(back)}, delay=delay, settle_pause=settle_pause, pause=pause,
        progress=progress, should_stop=should_stop, result=res,
    )


# ------------------------------------------------------------------------------------------ the report
def _name(ch) -> str:
    return esc(getattr(ch, "title", None) or str(getattr(ch, "id", "?")))


def report_lines(res: CrossResult, *, what: str = "the new copies", back: bool = False) -> list:
    """Lines (HTML) for the end of a repost / shift. `what`: where the links point now. `back`: they were pointed back."""
    out: list = []
    done = [c for c in res.channels if not c.skipped]
    if res.relinked:
        n = len(res.changed_channels)
        out.append(
            f"🔗 {res.links} link(s) in {res.relinked} post(s) of {n} channel(s) "
            f"{'point back at the original posts' if back else 'now point at ' + what}."
        )
        if n > 1 or (n == 1 and len(done) > 1):
            for c in res.changed_channels[:12]:
                out.append(f"• {_name(c.channel)}: {c.links} link(s) in {c.relinked} post(s)")
    elif done and not res.stopped and not any(c.aborted or c.failed for c in done):
        out.append(f"🔗 Checked {len(done)} channel(s): no link there needed changing.")
    if res.not_allowed:
        names = ", ".join(_name(c.channel) for c in res.channels if c.not_allowed)
        out.append(
            f"{res.not_allowed} post(s) with such a link were made by somebody else and the bot may not edit them "
            f"({names}). Give the bot the “Edit messages of others” right and press “🔗 Update links again”."
        )
    failed = res.failed
    if failed:
        sample = ", ".join(f"{_name(c)} #{mid} {esc(name)}" for c, mid, name in failed[:5])
        out.append(f"{len(failed)} post(s) could not get their links updated ({sample}{'…' if len(failed) > 5 else ''}).")
        if any(name in ("MessageIdInvalidError", "MessageAuthorRequiredError") for _, _, name in failed):
            out.append(
                "Telegram refuses edits to posts that other bots (or other people) made or put buttons on: those "
                "links have to be changed by their owner."
            )
    for c in res.channels:
        if c.aborted:
            out.append(f"Gave up on {_name(c.channel)} ({esc(c.aborted)}).")
    skipped = res.skipped
    if skipped:
        out.append("Not looked at: " + "; ".join(f"{_name(c.channel)} ({esc(c.skipped)})" for c in skipped[:8]))
    held = sum(c.held_back for c in res.channels)
    if held:
        out.append(f"{held} post(s) are held back by Telegram (copyright notice) and were left alone.")
    if res.stopped:
        out.append("⏹ Stopped before every channel was done - press “🔗 Update links again” to finish.")
    return out


def progress_text(res: CrossResult, *, title: str = "Pointing links at the new copies") -> str:
    """The line shown while a run is going on."""
    cur = res.current
    where = f" - {_name(cur.channel)}" if cur is not None else ""
    step = f" ({res.index} of {res.total})" if res.total else ""
    if cur is not None and res.phase == "scan" and cur.top:
        detail = f"\nReading posts {cur.scanned} of {cur.top}…"
    elif cur is not None and res.phase == "edit":
        detail = f"\nChanging links: {cur.relinked} of {cur.found - cur.not_allowed} post(s) done…"
    else:
        detail = ""
    return f"🔗 {title}{step}{where}{detail}\n{res.links} link(s) changed so far."
