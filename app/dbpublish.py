"""Automatic publishing: the autorename bot copies a renamed episode into the Database Channel and says so in the
communication chat; this bot then makes the post for it in the channel that message names.

The message of the autorename bot (it writes the Channel and Episode lines from the format the episode was renamed with):

    File uploaded to the Database Channel
    Episode: 05
    Channel: -1001234567890
    File: Black Clover - S2 E05.mkv
    Post: 1234          <- the copy in the Database Channel (without it: the latest post of that channel)
    Ref: 3f9a1c         <- repeated in every answer

  1. The message arrives -> this bot answers "received".
  2. It looks at that post of the Database Channel and waits for the 🔁 Share URL button the file-store bot adds to it a
     few seconds later (never less than MIN_SHARE_URL_WAIT seconds). It looks through the bot; when the bot cannot see
     the post or its buttons, the userbot looks as well (see userbot.UserbotReader).
  3. The post is made from two templates the owner edits with /autopublish: the message text ("Episode {episode}" and
     so on) and the button name ("Download Episode {episode}"). The button opens the file link of the Share URL button.
     It goes to the channel the message names - nothing is guessed from the file name.
  4. "Post published successfully" - or "Publication failed: <why>" - goes back to the communication chat.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

from telethon import errors, types
from telethon.helpers import add_surrogate, del_surrogate

from .buttons import norm_url
from .common import Ctx, send_post
from .config import MIN_SHARE_URL_WAIT
from .db import utcnow
from .linkswap import apply_span_replacements
from .replace_engine import fetch_messages, get_top_id, is_skippable
from .tgutil import button_url, de_entities, explain_rpc, peer_of, post_link, ser_entities
from .userbot import UserbotError

log = logging.getLogger(__name__)

POLL_SECONDS = 3.0  # between two looks at the post while the Share URL button is awaited
BOT_ONLY_SECONDS = 10.0  # for this long only the bot looks (the button is not there yet anyway); then the userbot looks too
KEEP_NOTICES = 200  # notices remembered, so one that is delivered twice is answered once

SETTING_KEY = "autopublish"  # where the templates live (db.get_setting)
DEFAULT_TEXT = "Episode {episode}\n\nClick the button below to download 👇"
DEFAULT_BUTTON = "Download Episode {episode}"
TEXT_MAX = 3000  # of the message text template (a post may have 4096 characters)
BUTTON_MAX = 60  # of the button name template
BUTTON_SHOWN = 64  # a button name is cut at this length when the post is made

_NOTICE_RX = re.compile(r"(?i)file\s+uploaded\s+to\s+the\s+database\s+channel")
_SHARE_RX = re.compile(r"(?i)\bshare\s*url\b")
_SHARE_LINK_RX = re.compile(r"(?i)^https?://(?:t\.me|telegram\.me|telegram\.dog)/share(?:/url)?\?(.*)$")
# {episode}, {episode number}, {episode_number}, {channel} (also {channel name}); spaces inside the braces do not matter
_VAR_RX = re.compile(r"\{\s*(?P<name>episode(?:[\s_]*(?:number|num))?|channel(?:[\s_]*(?:name|title))?)\s*\}", re.I)
_ANY_BRACES_RX = re.compile(r"\{[^{}\n]{0,40}\}")


class Failure(Exception):
    """Why a post could not be published: the text goes to the communication chat as it is."""


# ---------------------------------------------------------------------------------------------------- the notice
@dataclass
class Notice:
    file: str = ""  # the name the autorename bot gave the file
    episode: str = ""  # its episode number, as the autorename bot wrote it ("05": the zeros stay)
    channel: Optional[int] = None  # the channel to post in, as written (-100…); None when the message names none
    channel_raw: str = ""  # what stood after "Channel:" (shown when it is not an id)
    post_id: Optional[int] = None  # the copy in the Database Channel (None: the latest post is meant)
    ref: str = ""  # a token the answers repeat, so the autorename bot can tell which notice they belong to


def chat_number(raw: Optional[str]) -> Optional[int]:
    """A chat id from what a person or a bot wrote: -1001234567890, 1234567890 or a t.me/c/1234567890/5 link."""
    raw = (raw or "").strip()
    m = re.search(r"(?i)t\.me/c/(\d+)", raw)
    if m:
        return -1_000_000_000_000 - int(m.group(1))
    m = re.match(r"(-?\d{1,16})(?!\d)", raw)
    return int(m.group(1)) if m else None


def parse_notice(text: Optional[str]) -> Optional[Notice]:
    """The message the autorename bot writes ("File uploaded to the Database Channel", then Episode / Channel / File /
    Post / Ref lines); None for anything else."""
    if not _NOTICE_RX.search(text or ""):
        return None

    def field_of(label: str) -> str:
        m = re.search(rf"(?im)^\s*{label}\s*:\s*(.+?)\s*$", text or "")
        return m.group(1).strip() if m else ""

    post = field_of("post")
    channel_raw = field_of("channel")
    return Notice(
        file=field_of("file"),
        episode=" ".join(field_of("episode").split())[:30],
        channel=chat_number(channel_raw),
        channel_raw=channel_raw[:60],
        post_id=int(post) if post.isdigit() else None,
        ref=field_of("ref")[:40],
    )


def _tail(notice: Optional[Notice]) -> str:
    return f"\nRef: {notice.ref}" if notice is not None and notice.ref else ""


def received_text(notice: Optional[Notice] = None) -> str:
    return "📥 Message received - checking the Database Channel…" + _tail(notice)


def failed_text(reason: str, notice: Optional[Notice] = None, file: str = "") -> str:
    reason = " ".join((reason or "unknown reason").split())
    name = file or (notice.file if notice is not None else "")
    return f"❌ Publication failed: {reason[:600]}" + (f"\nFile: {name}" if name else "") + _tail(notice)


@dataclass
class Published:
    channel: Any
    link: str  # the new post
    label: str  # the button that got the file link
    url: str  # ... and that link
    file: str
    episode: str = ""
    notes: list = field(default_factory=list)


def published_text(p: Published, notice: Optional[Notice] = None) -> str:
    lines = [
        "✅ Post published successfully",
        f"Channel: {p.channel.title}",
        f"Post: {p.link}",
        f"Button: {p.label} → {p.url}",
    ]
    if p.episode:
        lines.append(f"Episode: {p.episode}")
    lines += [f"ℹ️ {n}" for n in p.notes]
    if p.file:
        lines.append(f"File: {p.file}")
    return "\n".join(lines) + _tail(notice)


# ------------------------------------------------------------------------------------------------ chat ids, names
def channel_id_of(value: int) -> int:
    """The id the database keeps for a channel, from the id as Telegram apps write it (-100…); a bare id stays as it is."""
    return -value - 1_000_000_000_000 if value <= -1_000_000_000_000 else abs(value)


def marked_chat_id(value: int) -> int:
    """A chat id the way Telethon reports it in events: a bare channel id gets its -100 in front, the rest stays."""
    return -1_000_000_000_000 - value if value > 0 else value


def norm(text: Optional[str]) -> str:
    """Lower case, no punctuation, single spaces: "Black-Clover_S2!" -> "black clover s2"."""
    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text or "").casefold()).split())


def strip_ext(name: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "")


# --------------------------------------------------------------------------------------- the two templates
def _variable(name: str) -> str:
    return "episode" if name.lower().startswith("episode") else "channel"


def uses(template: Optional[str], variable: str) -> bool:
    """Does the template have {episode} (or {channel}) in it, by any of its spellings?"""
    return any(_variable(m.group("name")) == variable for m in _VAR_RX.finditer(template or ""))


def unknown_variables(template: Optional[str]) -> list:
    """The {words} of a template that are none of the variables ({ep}, {number} ...): they would end up in the post as typed."""
    return [m.group(0) for m in _ANY_BRACES_RX.finditer(template or "") if not _VAR_RX.fullmatch(m.group(0))]


def fill(template: Optional[str], values: dict) -> str:
    """The template with its variables replaced (values: {"episode": "05", "channel": "Black Clover"})."""
    return _VAR_RX.sub(lambda m: str(values.get(_variable(m.group("name")), "")), template or "")


def fill_text(text: Optional[str], entities: Optional[list], values: dict) -> tuple:
    """(text, entities) of the message template with its variables replaced; bold, links and the other formatting stay
    on the words they covered (offsets are UTF-16 units, as Telegram counts them)."""
    sur = add_surrogate(text or "")
    repls = [
        (m.start(), m.end(), add_surrogate(str(values.get(_variable(m.group("name")), ""))))
        for m in _VAR_RX.finditer(sur)
    ]
    if not repls:
        return text or "", list(entities or [])
    new_sur, new_ents = apply_span_replacements(sur, de_entities(entities), repls)
    return del_surrogate(new_sur), ser_entities(new_ents)


@dataclass
class Templates:
    text: str = DEFAULT_TEXT
    entities: list = field(default_factory=list)  # of the text: bold, links ... as ser_entities makes them
    button: str = DEFAULT_BUTTON
    custom_text: bool = False  # False: the default one is in use
    custom_button: bool = False


async def load_templates(db) -> Templates:
    raw = await db.get_setting(SETTING_KEY) or {}
    t = Templates()
    if isinstance(raw.get("text"), str) and raw["text"].strip():
        t.text, t.entities, t.custom_text = raw["text"], list(raw.get("entities") or []), True
    if isinstance(raw.get("button"), str) and raw["button"].strip():
        t.button, t.custom_button = raw["button"], True
    return t


async def save_templates(db, *, text: Optional[str] = None, entities: Optional[list] = None, button: Optional[str] = None) -> None:
    """Keep a new message text (with its formatting) and / or button name; what is not given stays as it was."""
    raw = dict(await db.get_setting(SETTING_KEY) or {})
    if text is not None:
        raw["text"], raw["entities"] = text, list(entities or [])
    if button is not None:
        raw["button"] = button
    await db.set_setting(SETTING_KEY, raw)


async def reset_templates(db, *, text: bool = False, button: bool = False) -> None:
    """Go back to the default message text and / or button name."""
    raw = dict(await db.get_setting(SETTING_KEY) or {})
    if text:
        raw.pop("text", None)
        raw.pop("entities", None)
    if button:
        raw.pop("button", None)
    if raw:
        await db.set_setting(SETTING_KEY, raw)
    else:
        await db.delete_setting(SETTING_KEY)


def render_post(t: Templates, values: dict) -> tuple:
    """(text, entities, button name) of a post made from the templates."""
    text, entities = fill_text(t.text, t.entities, values)
    label = " ".join(fill(t.button, values).split())[:BUTTON_SHOWN]
    return text, entities, label


# --------------------------------------------------------------------------------------- the post and its button
def is_file(msg: Any) -> bool:
    return getattr(msg, "document", None) is not None


def share_button(markup: Any) -> Optional[tuple]:
    """(text, link) of the first link button called "Share URL" (the 🔁 in front does not matter), else None."""
    if not isinstance(markup, types.ReplyInlineMarkup):
        return None
    for row in markup.rows:
        for b in row.buttons:
            url = button_url(b)
            if url and _SHARE_RX.search(getattr(b, "text", "") or ""):
                return b.text, url
    return None


def file_link(url: str) -> str:
    """The link the file store means. A "Share URL" button usually opens Telegram's share sheet for the file link
    (t.me/share/url?url=<file link>): the file link is what a post's button needs, so that wrapper is taken off.
    A share-sheet link that holds no link of its own gives "" (there is nothing to download from it)."""
    url = (url or "").strip()
    m = _SHARE_LINK_RX.match(url)
    if not m:
        return url
    inner = re.search(r"(?:^|&)url=(.*?)(?=&text=|$)", m.group(1))
    link = inner.group(1) if inner else ""
    if "%" in link:
        link = unquote(link)
    return link if re.match(r"(?i)^(https?://|tg://)\S+$", link) else ""


def name_of(msg: Any) -> str:
    f = getattr(msg, "file", None)
    return (getattr(f, "name", None) or "") if f is not None else ""


# ---------------------------------------------------------------------------------------------------- the engine
@dataclass
class Seen:
    """One look at the post: the message as one account saw it, the Share URL button on it (if any), and who looked."""

    msg: Any
    found: Optional[tuple]
    via: str  # "bot" | "userbot"


@dataclass
class Look:
    """What survives from one look to the next while the button is awaited."""

    reader: Any = None  # the userbot's reader, once it was needed (kept open until the wait is over)
    reader_failed: bool = False
    notes: dict = field(default_factory=dict)  # why something could not be read -> shown if everything fails


class AutoPublisher:
    def __init__(self, ctx: Ctx, *, sleep=asyncio.sleep, clock=time.monotonic):
        self.ctx = ctx
        self._sleep, self._clock = sleep, clock
        self.lock = asyncio.Lock()  # one post at a time
        self._handled: "OrderedDict[int, None]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.ctx.cfg.auto_publish

    # --------------------------------------------------------------------------------------------- entry point
    async def handle(self, event: Any) -> None:
        """A message of the autorename bot in the communication chat."""
        notice = parse_notice(getattr(event, "raw_text", None))
        if notice is None or event.id in self._handled:
            return  # something else it says, or the same notice again
        self._handled[event.id] = None
        while len(self._handled) > KEEP_NOTICES:
            self._handled.popitem(last=False)
        await self._answer(event, received_text(notice))
        async with self.lock:
            try:
                done = await self.publish(notice)
                text = published_text(done, notice)
            except Failure as f:
                log.info("automatic publishing: %s", f)
                text = failed_text(str(f), notice)
            except Exception as e:  # noqa: BLE001 - the autorename bot must always get an answer
                log.exception("automatic publishing failed")
                text = failed_text(f"something went wrong inside the bot ({type(e).__name__}: {str(e)[:150]})", notice)
        await self._answer(event, text)

    async def _answer(self, event: Any, text: str) -> None:
        try:
            await event.reply(text, parse_mode=None, link_preview=False)
        except Exception as e:  # noqa: BLE001
            log.warning("could not answer in the communication chat: %s", e)

    async def check(self) -> None:
        """At start-up: say in the log what the bot cannot reach."""
        if not self.enabled:
            return
        for name, ref in (("COMMUNICATION_CHAT", self.ctx.cfg.comm_chat), ("DB_CHANNEL", self.ctx.cfg.db_channel)):
            try:
                await self.ctx.client.get_input_entity(ref)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot reach %s %s: %s (add the bot there as an admin)", name, ref, e)

    # --------------------------------------------------------------------------------------------------- steps
    async def publish(self, notice: Notice) -> Published:
        ch = await self._target_channel(notice)  # first: no use waiting for a button when there is nowhere to post
        templates = await load_templates(self.ctx.db)
        if not notice.episode and (uses(templates.text, "episode") or uses(templates.button, "episode")):
            raise Failure(
                "The message has no “Episode:” line, and the text or the button name of the post uses {episode} (see /autopublish)."
            )
        dbch = await self._database_channel()
        seen = await self._wait_for_share(dbch, notice)
        name = name_of(seen.msg) or notice.file
        if notice.post_id is None and notice.file and name_of(seen.msg) and norm(strip_ext(name)) != norm(strip_ext(notice.file)):
            raise Failure(
                f"The latest post of the Database Channel is the file “{name}”, not “{notice.file}” that was reported - "
                "nothing was published."
            )
        link = norm_url(file_link(seen.found[1]))
        if not link:
            raise Failure(f"The 🔁 Share URL button of that post has a link I cannot use: {seen.found[1][:150]}")
        done = await self._post(ch, templates, notice, link, name)
        if seen.via == "userbot":
            done.notes.append("The Share URL button was read through the userbot.")
        return done

    async def _target_channel(self, notice: Notice) -> Any:
        """The channel the message names: connected to this bot, switched on, and not the Database Channel itself."""
        if notice.channel is None:
            if notice.channel_raw:
                raise Failure(f"The message names “{notice.channel_raw}” as the channel, which is not a channel id (like -1001234567890).")
            raise Failure(
                "The message does not say which channel to post in: it has no “Channel:” line "
                "(the Channel option of the format the episode was renamed with)."
            )
        cid = channel_id_of(notice.channel)
        if cid == channel_id_of(self.ctx.cfg.db_channel):
            raise Failure("The channel in the message is the Database Channel itself - the post has to go to the channel of the series.")
        ch = await self.ctx.db.get_channel(cid)
        if ch is None or not ch.active:
            raise Failure(
                f"The channel {notice.channel} is not connected to this bot (/channels lists the connected ones, /addchannel adds one), "
                "so I cannot post there."
            )
        return ch

    async def _database_channel(self) -> Any:
        cid = channel_id_of(self.ctx.cfg.db_channel)
        ch = await self.ctx.db.get_channel(cid)
        if ch is not None:
            return ch
        try:  # not one of the registered channels: ask Telegram
            ent = await self.ctx.client.get_entity(types.PeerChannel(cid))
        except Exception as e:  # noqa: BLE001
            raise Failure(f"I cannot open the Database Channel ({type(e).__name__}). Is the bot an admin there?")
        if not isinstance(ent, types.Channel):
            raise Failure("DB_CHANNEL is not a channel.")
        return ent

    # ------------------------------------------------------------------------------------ waiting for the button
    async def _wait_for_share(self, ch: Any, notice: Notice) -> Seen:
        """Look at the Database Channel post until it shows its Share URL button. The file-store bot adds that button a
        few seconds after the post appears, so "no button" is only an answer once the whole wait is over."""
        wait = max(MIN_SHARE_URL_WAIT, self.ctx.cfg.share_url_wait)
        started = self._clock()
        look = Look()
        seen: Optional[Seen] = None
        try:
            while True:
                seen = await self._look(ch, notice.post_id, look, userbot_too=self._clock() - started >= BOT_ONLY_SECONDS)
                if seen is not None and seen.found:
                    return seen
                if seen is not None and not is_file(seen.msg):
                    raise Failure(f"The post of the Database Channel is not a file ({self._kind(seen.msg)}), so there is nothing to publish.")
                if self._clock() - started >= wait:
                    break
                await self._sleep(POLL_SECONDS)
        finally:
            if look.reader is not None:
                await look.reader.close()
        raise Failure(self._no_button(seen, look, wait))

    @staticmethod
    def _kind(msg: Any) -> str:
        if getattr(msg, "photo", None) is not None:
            return "a photo"
        return "text" if (getattr(msg, "message", "") or "").strip() else "an unsupported message"

    def _no_button(self, seen: Optional[Seen], look: Look, wait: float) -> str:
        secs = int(wait)
        if seen is None:
            why = "; ".join(look.notes.values()) or "it is not there"
            return f"I could not read the post of the Database Channel ({why})."
        ub = self.ctx.userbot
        if look.reader is not None or (ub is not None and ub.enabled and not look.reader_failed):
            where = "through the bot and through the userbot"
        else:
            where = "through the bot (no userbot is set up or reachable)"
        extra = f" {'; '.join(look.notes.values())}." if look.notes else ""
        return f"No 🔁 Share URL button was found on that post of the Database Channel after waiting {secs} seconds (looked {where}).{extra}"

    async def _look(self, ch: Any, post_id: Optional[int], look: Look, *, userbot_too: bool) -> Optional[Seen]:
        """One look: through the bot; when the bot cannot read the post, or sees no button once the file-store bot has had
        its time, through the userbot as well."""
        msg = await self._bot_read(ch, post_id, look)
        found = share_button(msg.reply_markup) if msg is not None and is_file(msg) else None
        if found:
            return Seen(msg, found, "bot")
        if msg is None or (userbot_too and is_file(msg)):
            other = await self._userbot_read(ch, post_id, look)
            if other is not None:
                return Seen(other, share_button(other.reply_markup) if is_file(other) else None, "userbot")
        return Seen(msg, None, "bot") if msg is not None else None

    async def _bot_read(self, ch: Any, post_id: Optional[int], look: Look) -> Any:
        try:
            if post_id:
                msgs = [m for m in await fetch_messages(self.ctx.client, peer_of(ch), [post_id]) if not is_skippable(m)]
                return msgs[0] if msgs else None
            return await self._bot_latest(ch)
        except errors.RPCError as e:
            look.notes["bot"] = f"Telegram refused the bot ({type(e).__name__})"
            return None

    async def _bot_latest(self, ch: Any) -> Any:
        """The newest post of a channel, as far as a bot can tell: look upwards from the newest post My posts knows."""
        client, peer = self.ctx.client, peer_of(ch)
        first = await self.ctx.db.max_saved_id(ch.id)
        if not first:
            top = await get_top_id(client, peer) or 1
            first = max(1, top - 200)
        newest = None
        for _ in range(8):
            msgs = [m for m in await fetch_messages(client, peer, list(range(first, first + 100))) if not is_skippable(m)]
            if not msgs:
                break
            newest = max(msgs, key=lambda m: m.id)
            first = newest.id + 1
        return newest

    async def _userbot_read(self, ch: Any, post_id: Optional[int], look: Look) -> Any:
        ub = self.ctx.userbot
        if look.reader is None and not look.reader_failed:
            reader = await ub.reader_for(ch) if ub is not None else None
            if reader is None:
                look.reader_failed = True
                if ub is not None and ub.enabled:
                    look.notes["userbot"] = f"the userbot is not connected: {ub.last_error or 'unknown reason'}"
            else:
                look.reader = reader
        if look.reader is None:
            return None
        try:
            msgs = [m for m in await look.reader.read(post_id) if not is_skippable(m)]
        except (UserbotError, errors.RPCError) as e:
            look.reader_failed = True
            look.notes["userbot"] = f"the userbot could not read it: {e}"
            await look.reader.close()
            look.reader = None
            return None
        return msgs[0] if msgs else None

    # ---------------------------------------------------------------------------------------------- the new post
    async def _post(self, ch: Any, templates: Templates, notice: Notice, link: str, name: str) -> Published:
        db = self.ctx.db
        text, entities, label = render_post(templates, {"episode": notice.episode, "channel": ch.title or ""})
        if not text.strip() or not label:
            raise Failure("The message text or the button name of the post comes out empty - check /autopublish.")
        post = await db.create_post(
            channel_id=ch.id, created_by=0, status="draft", source="bot", text=text, entities=entities,
            buttons=[[{"t": label, "u": link}]], media_kind=None, media_file_id=None, link_preview=False,
        )
        try:
            sent = await send_post(self.ctx.client, peer_of(ch), post)
        except errors.RPCError as e:
            await db.delete_post(post.id)
            raise Failure(f"Telegram would not let the bot post in “{ch.title}”: {explain_rpc(e)}")
        except Exception:
            await db.delete_post(post.id)
            raise
        await db.release_slot(ch.id, sent.id, keep_pid=post.id)  # the sync may have picked the message up a moment ago
        await db.update_post(post.id, status="sent", message_id=sent.id, sent_at=utcnow())
        return Published(
            channel=ch, link=post_link(ch, sent.id), label=label, url=link, file=name or notice.file, episode=notice.episode
        )
