"""Make sure the buttons shown in a channel are the ones we saved - and fix them when they are not.

Used by /repost (every new post is read back right after it is created) and by "Check buttons" in My posts,
which repairs posts whose buttons are saved in the bot but are missing in the channel.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from telethon import errors, types

from .replace_engine import CHUNK, fetch_messages, is_skippable
from .tgutil import build_markup, button_url, edit_raw, flood_retry, peer_of

log = logging.getLogger(__name__)

ABORT_AFTER = 5  # stop repairing after this many failures in a row
# A throw-away keyboard. If Telegram answers "message not modified" to the real keyboard (it believes the post
# already has it), we first show this one, then the real one: two real changes.
NUDGE_ROWS = [[{"t": "…", "u": "https://t.me/telegram"}]]


class ButtonsNotShown(RuntimeError):
    """Telegram accepted the edit, but the post still does not show the buttons it should."""


def link_buttons(markup) -> list:
    """[(text, url)] of the link buttons of a keyboard, in reading order."""
    out: list = []
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in markup.rows:
            for b in row.buttons:
                u = button_url(b)
                if u is not None:
                    out.append((getattr(b, "text", ""), u))
    return out


def link_urls(markup) -> list:
    return [u for _, u in link_buttons(markup)]


async def live_urls(client, peer, message_id: int) -> Optional[list]:
    """The link-button URLs the channel shows on a post right now; None if the post is gone."""
    msgs = await fetch_messages(client, peer, [message_id])
    m = msgs[0] if msgs else None
    if is_skippable(m):
        return None
    return link_urls(m.reply_markup)


async def ensure_markup(client, peer, message_id: int, markup, *, pause: float = 0.7) -> str:
    """Make post `message_id` show the link buttons of `markup`, and check it by reading the post back.

    Returns "fine" (it already did), "set" (one edit was enough) or "nudged" (needed the two-step edit).
    Raises ButtonsNotShown if the buttons still are not there, LookupError if the post no longer exists.
    """
    want = link_urls(markup)
    if not want:
        return "fine"
    live = await live_urls(client, peer, message_id)
    if live is None:
        raise LookupError(f"post {message_id} no longer exists")
    if live == want:
        return "fine"

    try:
        await flood_retry(lambda: edit_raw(client, peer, message_id, markup=markup))
    except errors.MessageNotModifiedError:
        pass  # Telegram thinks the keyboard is already there - checked below
    if pause:
        await asyncio.sleep(pause)
    if await live_urls(client, peer, message_id) == want:
        return "set"

    log.info("post %s: the edit did not make the buttons show - trying the two-step edit", message_id)
    for step in (build_markup(NUDGE_ROWS), markup):
        try:
            await flood_retry(lambda step=step: edit_raw(client, peer, message_id, markup=step))
        except errors.MessageNotModifiedError:
            pass
        if pause:
            await asyncio.sleep(pause)
    if await live_urls(client, peer, message_id) == want:
        return "nudged"
    raise ButtonsNotShown(f"post {message_id} still does not show its buttons")


# ------------------------------------------------------------------------------------ checking
@dataclass
class ButtonScan:
    channel: object
    total: int = 0  # saved posts that have link buttons
    fine: int = 0
    bad: list = field(default_factory=list)  # [(post, live urls, wanted urls)]
    gone: list = field(default_factory=list)  # posts that no longer exist in the channel


async def scan_buttons(
    client, ch, posts: list, *, progress: Optional[Callable[[ButtonScan], Awaitable[None]]] = None
) -> ButtonScan:
    """Compare the saved link buttons of `posts` with what the channel shows."""
    scan = ButtonScan(channel=ch)
    peer = peer_of(ch)
    wanted = []
    for p in posts:
        if not p.message_id:
            continue
        urls = link_urls(build_markup(p.buttons))
        if urls:
            wanted.append((p, urls))
    scan.total = len(wanted)
    for i in range(0, len(wanted), CHUNK):
        part = wanted[i : i + CHUNK]
        msgs = await fetch_messages(client, peer, [p.message_id for p, _ in part])
        for (p, urls), m in zip(part, msgs):
            if is_skippable(m):
                scan.gone.append(p)
            elif link_urls(m.reply_markup) == urls:
                scan.fine += 1
            else:
                scan.bad.append((p, link_urls(m.reply_markup), urls))
        if progress:
            await progress(scan)
    return scan


@dataclass
class FixResult:
    fixed: int = 0
    nudged: int = 0  # of those, how many needed the two-step edit
    already: int = 0  # turned out to be fine when looked at again
    gone: int = 0
    failed: list = field(default_factory=list)  # [(message id, error name)]
    aborted: Optional[str] = None
    stopped: bool = False


async def fix_buttons(
    client,
    ch,
    bad: list,
    *,
    delay: float = 1.2,
    pause: float = 0.7,
    progress: Optional[Callable[[FixResult], Awaitable[None]]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> FixResult:
    """Put the saved buttons back on the posts in `bad` (the first item of each entry is the saved post)."""
    res = FixResult()
    peer = peer_of(ch)
    streak = 0
    for entry in bad:
        post = entry[0]
        if should_stop and should_stop():
            res.stopped = True
            break
        try:
            how = await ensure_markup(client, peer, post.message_id, build_markup(post.buttons), pause=pause)
        except LookupError:
            res.gone += 1
            continue
        except Exception as e:  # one bad post must not stop the others
            name = type(e).__name__
            log.warning("could not fix the buttons of %s: %s %s", post.message_id, name, e)
            res.failed.append((post.message_id, name))
            streak += 1
            if streak >= ABORT_AFTER:
                res.aborted = name
                break
            await asyncio.sleep(delay)
            continue
        streak = 0
        if how == "fine":
            res.already += 1
        else:
            res.fixed += 1
            if how == "nudged":
                res.nudged += 1
        if progress:
            await progress(res)
        await asyncio.sleep(delay)
    return res
