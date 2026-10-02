"""Swap a username inside t.me links: button links, hyperlinks and typed links in message text.

Only the username part of a link changes; everything after it (?start=...) is left untouched.
Offsets of message entities are UTF-16 based, so text is handled in Telethon's 'surrogate' form.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Optional

from telethon import types
from telethon.helpers import add_surrogate, del_surrogate

from .tgutil import button_url, is_callback_button, with_url

USERNAME_RX = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def normalize_username(s: str) -> Optional[str]:
    s = (s or "").strip()
    m = re.match(r"(?i)^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@")
    return s if USERNAME_RX.match(s) else None


# ------------------------------------------------------------------------------- urls
_HTTP_RX = re.compile(
    r"^(?P<pre>(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/)(?P<name>[A-Za-z0-9_]+)(?P<rest>[/?#].*)?$",
    re.I | re.S,
)
_TG_RX = re.compile(r"^(?P<pre>tg://resolve\?(?:[^#]*&)?domain=)(?P<name>[A-Za-z0-9_]+)(?P<rest>[&#].*)?$", re.I | re.S)


def _split(url: str):
    for rx in (_HTTP_RX, _TG_RX):
        m = rx.match(url)
        if m:
            return m
    return None


def _is_post_tail(tail: str) -> bool:
    return bool(re.match(r"/\d", tail)) or bool(re.match(r"&post=\d", tail))


def classify_url(url: str, old: str) -> Optional[str]:
    """'link' (t.me/old?start=..), 'post' (t.me/old/123 - a channel post) or None."""
    m = _split(url or "")
    if not m or m.group("name").lower() != old.lower():
        return None
    return "post" if _is_post_tail(m.group("rest") or "") else "link"


def swap_url(url: str, old: str, new: str) -> str:
    m = _split(url or "")
    if not m or m.group("name").lower() != old.lower():
        return url
    return url[: m.start("name")] + new + url[m.end("name"):]


# ------------------------------------------------------------------- typed links in text
def _typed_regexes(old: str) -> list:
    n = re.escape(old)
    return [
        re.compile(
            r"(?<![\w@./-])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?P<name>" + n + r")(?![A-Za-z0-9_])",
            re.I,
        ),
        re.compile(r"(?<![\w@./-])tg://resolve\?(?:[^\s&#]*&)?domain=(?P<name>" + n + r")(?![A-Za-z0-9_])", re.I),
    ]


def find_typed_spans(sur: str, old: str, include_posts: bool) -> tuple:
    spans: list = []
    skipped = 0
    for rx in _typed_regexes(old):
        for m in rx.finditer(sur):
            s, e = m.span("name")
            if _is_post_tail(sur[e:e + 8]) and not include_posts:
                skipped += 1
                continue
            spans.append((s, e))
    spans.sort()
    return spans, skipped


def apply_span_replacements(sur: str, entities: list, repls: list) -> tuple:
    """repls: sorted, non-overlapping (start, end, new_text) in UTF-16 units.
    Returns (new_text, new_entities) with every entity offset/length kept consistent."""
    parts: list = []
    last = 0
    for s, e, nt in repls:
        parts.append(sur[last:s])
        parts.append(nt)
        last = e
    parts.append(sur[last:])
    new_text = "".join(parts)

    def mp(x: int) -> int:
        acc = 0
        for s, e, nt in repls:
            if x >= e:
                acc += len(nt) - (e - s)
            elif x > s:
                return s + acc + min(x - s, len(nt))
            else:
                break
        return x + acc

    out = []
    for ent in entities:
        cp = copy.copy(ent)
        o = mp(ent.offset)
        end = mp(ent.offset + ent.length)
        cp.offset = o
        cp.length = max(0, end - o)
        out.append(cp)
    return new_text, out


# --------------------------------------------------------------------- whole message
@dataclass
class MsgChange:
    message_id: int
    new_text: str
    new_entities: list
    new_markup: Optional[types.ReplyInlineMarkup]
    text_changed: bool
    entities_changed: bool
    n_buttons: int
    n_links: int
    n_typed: int
    mine: bool
    callbacks: bool


def compute_message_changes(msg, old: str, new: str, *, include_typed: bool = True, include_posts: bool = False) -> tuple:
    """Returns (MsgChange or None, number_of_skipped_post_links)."""
    text = msg.message or ""
    ents = list(msg.entities or [])
    skipped = 0
    n_typed = n_links = n_btn = 0

    # 1) links typed as plain text (this changes the text itself)
    sur = add_surrogate(text)
    new_sur = sur
    new_ents = [copy.copy(e) for e in ents]
    if include_typed and text:
        spans, sk = find_typed_spans(sur, old, include_posts)
        skipped += sk
        if spans:
            new_sur, new_ents = apply_span_replacements(sur, ents, [(s, e, new) for s, e in spans])
            n_typed = len(spans)

    # 2) hyperlinks (text with a link behind it)
    for e in new_ents:
        if isinstance(e, types.MessageEntityTextUrl):
            kind = classify_url(e.url, old)
            if kind == "link" or (kind == "post" and include_posts):
                e.url = swap_url(e.url, old, new)
                n_links += 1
            elif kind == "post":
                skipped += 1

    # 3) buttons
    new_markup = None
    has_cb = False
    mk = msg.reply_markup
    if isinstance(mk, types.ReplyInlineMarkup):
        rows = []
        for row in mk.rows:
            btns = []
            for b in row.buttons:
                url = button_url(b)
                if url is not None:
                    kind = classify_url(url, old)
                    if kind == "link" or (kind == "post" and include_posts):
                        btns.append(with_url(b, swap_url(url, old, new)))
                        n_btn += 1
                        continue
                    if kind == "post":
                        skipped += 1
                elif is_callback_button(b):
                    has_cb = True
                btns.append(b)
            rows.append(types.KeyboardButtonRow(btns))
        if n_btn:
            new_markup = types.ReplyInlineMarkup(rows)

    if not (n_typed or n_links or n_btn):
        return None, skipped
    return (
        MsgChange(
            message_id=msg.id,
            new_text=del_surrogate(new_sur),
            new_entities=new_ents,
            new_markup=new_markup,
            text_changed=bool(n_typed),
            entities_changed=bool(n_typed or n_links),
            n_buttons=n_btn,
            n_links=n_links,
            n_typed=n_typed,
            mine=bool(getattr(msg, "out", False)),
            callbacks=has_cb,
        ),
        skipped,
    )


def url_only_markup(markup, old: Optional[str] = None, new: Optional[str] = None, include_posts: bool = False):
    """Buttons for a copy of a post: link buttons only (other bots' buttons would be dead), with the username
    swapped when `old`/`new` are given. Returns (markup or None, number_of_buttons_not_copied)."""
    if not isinstance(markup, types.ReplyInlineMarkup):
        return None, 0
    rows, dropped = [], 0
    for row in markup.rows:
        btns = []
        for b in row.buttons:
            url = button_url(b)
            if url is None:
                dropped += 1
                continue
            if old and new:
                kind = classify_url(url, old)
                if kind == "link" or (kind == "post" and include_posts):
                    b = with_url(b, swap_url(url, old, new))
            btns.append(b)
        if btns:
            rows.append(types.KeyboardButtonRow(btns))
    return (types.ReplyInlineMarkup(rows) if rows else None), dropped
