"""/handleswap: change a username written in a post - "@bro" -> "@sis" - and nothing else.

Only usernames are touched: the words "@bro" in the text or caption (and in button names). Links, hyperlinks and the
links behind buttons are left exactly as they are, and so is "@bro" when it is part of a link or an e-mail address.
"""
from __future__ import annotations

import copy
import re
from typing import Optional

from telethon import types
from telethon.helpers import add_surrogate, del_surrogate

from .linkswap import MsgChange, apply_span_replacements
from .tgutil import button_url, is_callback_button, with_text

# a "@name" that follows one of these is part of something else: an e-mail (a@bro), a link (t.me/x@bro, ?u=@bro) ...
_NOT_BEFORE = r"\w@./=?&#%"
_PROTECTED = tuple(
    getattr(types, n) for n in ("MessageEntityUrl", "MessageEntityTextUrl", "MessageEntityEmail") if hasattr(types, n)
)


def mention_regex(name: str):
    """'@name' as a whole word (case does not matter): not '@name_bot', not 'x@name'."""
    return re.compile(rf"(?<![{_NOT_BEFORE}])@{re.escape(name)}(?![A-Za-z0-9_])", re.I)


def find_mention_spans(sur: str, entities, name: str) -> tuple:
    """([(start, end)], skipped) - where '@name' stands in `sur` (UTF-16 units). Mentions that sit inside a link or a
    hyperlink are not changed; `skipped` counts them."""
    guarded = [(e.offset, e.offset + e.length) for e in entities or [] if isinstance(e, _PROTECTED)]
    spans, skipped = [], 0
    for m in mention_regex(name).finditer(sur):
        s, e = m.span()
        if any(gs < e and s < ge for gs, ge in guarded):
            skipped += 1
            continue
        spans.append((s, e))
    return spans, skipped


def compute_handle_changes(msg, old: str, new: str) -> tuple:
    """(MsgChange or None, number of mentions skipped because they are part of a link) - same shape as
    linkswap.compute_message_changes, so the /replace engine can apply it."""
    text = msg.message or ""
    ents = list(msg.entities or [])
    sur = add_surrogate(text)
    spans, skipped = find_mention_spans(sur, ents, old) if text else ([], 0)
    new_sur, new_ents = sur, [copy.copy(e) for e in ents]
    if spans:
        new_sur, new_ents = apply_span_replacements(sur, ents, [(s, e, "@" + new) for s, e in spans])

    # the names on link buttons (their links stay)
    new_markup: Optional[types.ReplyInlineMarkup] = None
    n_btn, has_cb = 0, False
    mk = msg.reply_markup
    if isinstance(mk, types.ReplyInlineMarkup):
        rx = mention_regex(old)
        rows = []
        for row in mk.rows:
            btns = []
            for b in row.buttons:
                if button_url(b) is not None:
                    label, n = rx.subn("@" + new, getattr(b, "text", "") or "")
                    if n:
                        btns.append(with_text(b, label))
                        n_btn += n
                        continue
                elif is_callback_button(b):
                    has_cb = True
                btns.append(b)
            rows.append(types.KeyboardButtonRow(btns))
        if n_btn:
            new_markup = types.ReplyInlineMarkup(rows)

    if not (spans or n_btn):
        return None, skipped
    return (
        MsgChange(
            message_id=msg.id,
            new_text=del_surrogate(new_sur),
            new_entities=new_ents,
            new_markup=new_markup,
            text_changed=bool(spans),
            entities_changed=bool(spans),
            n_buttons=n_btn,
            n_links=0,
            n_typed=len(spans),
            mine=bool(getattr(msg, "out", False)),
            callbacks=has_cb,
        ),
        skipped,
    )
