"""Links that point at other posts of the same channel (t.me/name/115, t.me/c/123456/115, tg://...&post=115).

When a channel is reposted its posts get new message ids, so a link to post 115 has to point at the copy of post 115.
Only the digits of the message id change; everything around them (?single, ?comment=3, the visible text) stays.
Links can sit in three places: a hyperlink behind text, a link typed into the text, and a button.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Optional

from telethon import types
from telethon.helpers import add_surrogate, del_surrogate

from .linkswap import apply_span_replacements
from .tgutil import button_url, with_url

_HOST = r"(?:t\.me|telegram\.me|telegram\.dog)"
_NAME = r"[A-Za-z][A-Za-z0-9_]{2,31}"
# https://t.me/name/115   https://t.me/c/123456/115   (also t.me/s/name/115, the web preview)
_WEB = re.compile(
    rf"^(?:https?://)?(?:www\.)?{_HOST}/(?:s/)?(?P<seg>c/(?P<cid>\d+)|(?P<name>{_NAME}))/(?P<id>\d+)(?![A-Za-z0-9_])", re.I
)
_TYPED = re.compile(
    rf"(?<![\w@./=?&#-])(?:https?://)?(?:www\.)?{_HOST}/(?:s/)?(?:c/\d+|{_NAME})/\d+(?![A-Za-z0-9_])", re.I
)
# tg://resolve?domain=name&post=115   tg://privatepost?channel=123456&post=115
_TG = re.compile(r"^tg://(?P<kind>resolve|privatepost)\?(?P<query>[^#]*)", re.I)
_POST_PARAM = re.compile(r"(?<=[?&])post=(\d+)")


@dataclass(frozen=True)
class Ref:
    """A link to a post: which channel (username in lower case, or the numeric id) and where the id's digits sit."""

    name: Optional[str]
    cid: Optional[int]
    msg_id: int
    start: int  # the digits of the message id ...
    end: int
    seg_start: Optional[int] = None  # ... and the channel part in front of them ("name" or "c/123"); web links only


def parse_ref(url: str) -> Optional[Ref]:
    """What `url` points at, if it is a link to a post of some channel; None for every other link."""
    url = url or ""
    m = _WEB.match(url)
    if m:
        return Ref(
            name=m.group("name").lower() if m.group("name") else None,
            cid=int(m.group("cid")) if m.group("cid") else None,
            msg_id=int(m.group("id")),
            start=m.start("id"),
            end=m.end("id"),
            seg_start=m.start("seg"),
        )
    m = _TG.match(url)
    if m:
        params = dict(p.split("=", 1) for p in m.group("query").split("&") if "=" in p)
        post = _POST_PARAM.search(url)
        if post is None:
            return None
        if m.group("kind").lower() == "resolve" and re.fullmatch(r"[A-Za-z0-9_]+", params.get("domain", "")):
            return Ref(params["domain"].lower(), None, int(post.group(1)), post.start(1), post.end(1))
        if m.group("kind").lower() == "privatepost" and params.get("channel", "").isdigit():
            return Ref(None, int(params["channel"]), int(post.group(1)), post.start(1), post.end(1))
    return None


@dataclass
class PostLinks:
    """The links that point into one channel, and where its posts went: `ids` maps old message id -> new one.

    By default the copies are in the same channel (/repost): only the message id changes. With `target_id` the
    copies are in another channel (/shift): the channel part of the link changes too."""

    username: Optional[str]
    channel_id: int
    ids: dict = field(default_factory=dict)
    target_username: Optional[str] = None
    target_id: Optional[int] = None

    @classmethod
    def for_channel(cls, ch, ids: Optional[dict] = None) -> "PostLinks":
        return cls(username=(ch.username or None), channel_id=int(ch.id), ids=ids if ids is not None else {})

    # -------------------------------------------------------------------------------------- one url
    def ref_of(self, url: str) -> Optional[Ref]:
        """The reference in `url` if it points at a post of THIS channel."""
        r = parse_ref(url)
        if r is None:
            return None
        if r.name is not None and self.username and r.name == self.username.lower():
            return r
        if r.cid is not None and r.cid == self.channel_id:
            return r
        return None

    def edit_of(self, url: str) -> Optional[tuple]:
        """(start, end, new text) to apply to `url` so it points at the copy of its post; None if it stays as it is."""
        r = self.ref_of(url)
        if r is None or r.msg_id not in self.ids:
            return None
        new_id = str(self.ids[r.msg_id])
        if self.target_id is None:
            return r.start, r.end, new_id
        if r.seg_start is None:  # tg:// links are not moved to another channel
            return None
        channel = self.target_username or f"c/{self.target_id}"
        return r.seg_start, r.end, f"{channel}/{new_id}"

    def remap_url(self, url: str) -> str:
        e = self.edit_of(url)
        return url if e is None else url[: e[0]] + e[2] + url[e[1] :]

    # --------------------------------------------------------------------------------- whole message
    def count(self, text: str, entities, markup) -> int:
        """How many links in a message point at a post of this channel (whether or not the post has a new id)."""
        n = 0
        sur = add_surrogate(text or "")
        for m in _TYPED.finditer(sur):
            if self.ref_of(m.group(0)):
                n += 1
        for e in entities or []:
            if isinstance(e, types.MessageEntityTextUrl) and self.ref_of(e.url):
                n += 1
        if isinstance(markup, types.ReplyInlineMarkup):
            for row in markup.rows:
                for b in row.buttons:
                    u = button_url(b)
                    if u is not None and self.ref_of(u):
                        n += 1
        return n

    def remap_text(self, text: str, entities) -> tuple:
        """-> (text, entities, typed_links_changed, hyperlinks_changed). Offsets of all entities stay right."""
        ents = list(entities or [])
        sur = add_surrogate(text or "")
        repls = []
        for m in _TYPED.finditer(sur):
            e = self.edit_of(m.group(0))
            if e is not None:
                repls.append((m.start() + e[0], m.start() + e[1], e[2]))
        if repls:
            new_sur, new_ents = apply_span_replacements(sur, ents, repls)
        else:
            new_sur, new_ents = sur, [copy.copy(e) for e in ents]
        hyper = 0
        for e in new_ents:
            if isinstance(e, types.MessageEntityTextUrl):
                new_url = self.remap_url(e.url)
                if new_url != e.url:
                    e.url = new_url
                    hyper += 1
        return del_surrogate(new_sur), new_ents, len(repls), hyper

    def remap_markup(self, markup) -> tuple:
        """-> (markup, buttons_changed); the keyboard object is only rebuilt when a button changes."""
        if not isinstance(markup, types.ReplyInlineMarkup):
            return markup, 0
        rows, changed = [], 0
        for row in markup.rows:
            btns = []
            for b in row.buttons:
                u = button_url(b)
                new = self.remap_url(u) if u is not None else None
                if new is not None and new != u:
                    btns.append(with_url(b, new))
                    changed += 1
                else:
                    btns.append(b)
            rows.append(types.KeyboardButtonRow(btns))
        return (types.ReplyInlineMarkup(rows) if changed else markup), changed


@dataclass
class Relinked:
    """A message whose links to other posts of the channel were pointed at the copies."""

    text: str
    entities: list
    markup: Optional[types.ReplyInlineMarkup]
    text_changed: bool  # the text or its entities differ
    markup_changed: bool
    n_typed: int
    n_hyper: int
    n_buttons: int

    @property
    def links(self) -> int:
        return self.n_typed + self.n_hyper + self.n_buttons


def relink_message(msg, links: PostLinks) -> Optional[Relinked]:
    """The new text / entities / buttons of `msg` once its links are remapped; None when nothing changes."""
    text, entities, n_typed, n_hyper = links.remap_text(msg.message or "", msg.entities)
    markup, n_buttons = links.remap_markup(msg.reply_markup)
    if not (n_typed or n_hyper or n_buttons):
        return None
    return Relinked(
        text=text,
        entities=entities,
        markup=markup,
        text_changed=bool(n_typed or n_hyper),
        markup_changed=bool(n_buttons),
        n_typed=n_typed,
        n_hyper=n_hyper,
        n_buttons=n_buttons,
    )


def resolve_chain(step: dict) -> dict:
    """Several reposts in a row: {1: 5, 5: 9} -> {1: 9, 5: 9} (a post that moved twice points at its latest copy)."""
    out = {}
    for old in step:
        cur, seen = old, 0
        while cur in step and seen < 100:
            cur, seen = step[cur], seen + 1
        out[old] = cur
    return out
