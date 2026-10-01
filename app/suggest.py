"""Auto-suggest the next episode batch: 'Download Episodes 01 to 20' -> 'Download Episodes 21 to 40'."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Optional

from telethon.helpers import add_surrogate, del_surrogate

from .linkswap import apply_span_replacements
from .tgutil import de_entities, ser_entities

RANGE_RX = re.compile(r"(?P<a>\d+)(?P<sep>\s*(?:to|[-–—~&])\s*)(?P<pre>(?:ep?\.?\s*)?)(?P<b>\d+)", re.I)
SINGLE_RX = re.compile(r"(?<![\w.])(?P<label>episodes?|eps?\.?|e)(?P<gap>\s*[-:#]?\s*)(?P<n>\d+)(?!\d)", re.I)


@dataclass
class Advance:
    new_text: str  # the whole string with the numbers moved on
    old_span: str  # e.g. "01 to 20"
    new_span: str  # e.g. "21 to 40"


def advance_plain(s: str) -> Optional[Advance]:
    """Move the first episode range (or single episode) in `s` on to the next batch."""
    for m in RANGE_RX.finditer(s or ""):
        a_s, b_s = m.group("a"), m.group("b")
        a, b = int(a_s), int(b_s)
        if a > b or b - a > 5000:
            continue
        step = b - a + 1
        width = max(len(a_s), len(b_s)) if (a_s.startswith("0") or b_s.startswith("0")) else 0
        na, nb = str(b + 1).zfill(width), str(b + step).zfill(width)
        new_span = na + s[m.end("a"):m.start("b")] + nb
        return Advance(
            new_text=s[: m.start("a")] + new_span + s[m.end("b"):],
            old_span=s[m.start("a"):m.end("b")],
            new_span=new_span,
        )
    for m in SINGLE_RX.finditer(s or ""):
        n_s = m.group("n")
        width = len(n_s) if n_s.startswith("0") else 0
        nn = str(int(n_s) + 1).zfill(width)
        return Advance(
            new_text=s[: m.start("n")] + nn + s[m.end("n"):],
            old_span=s[m.start("label"):m.end("n")],
            new_span=s[m.start("label"):m.start("n")] + nn,
        )
    return None


@dataclass
class Suggestion:
    text: str
    entities: list
    buttons: list
    media_kind: Optional[str]
    media_file_id: Optional[str]
    pending: list = field(default_factory=list)  # [(row, col)] of buttons that still need a link
    prev_label: str = ""
    new_label: str = ""
    source_post_id: int = 0


def build_suggestion(post) -> Optional[Suggestion]:
    rows: list = []
    pending: list = []
    first: Optional[Advance] = None
    prev_label = ""
    for row in post.buttons or []:
        new_row: list = []
        for b in row:
            if b.get("raw"):  # other bots' buttons (e.g. reactions) are not carried over
                continue
            b = dict(b)
            b.pop("s", None)
            adv = advance_plain(b.get("t", "")) if b.get("u") else None
            if adv:
                if first is None:
                    first, prev_label = adv, b["t"]
                b["t"] = adv.new_text
                b["u"] = None
                pending.append((len(rows), len(new_row)))
            new_row.append(b)
        if new_row:
            rows.append(new_row)
    if not pending or first is None:
        return None

    text = post.text or ""
    entities = copy.deepcopy(post.entities or [])
    sur = add_surrogate(text)
    repls: list = []
    pos = 0
    while first.old_span:
        i = sur.find(first.old_span, pos)
        if i < 0:
            break
        repls.append((i, i + len(first.old_span), first.new_span))
        pos = i + len(first.old_span)
    if repls:
        new_sur, new_ents = apply_span_replacements(sur, de_entities(entities), repls)
        text, entities = del_surrogate(new_sur), ser_entities(new_ents)

    return Suggestion(
        text=text,
        entities=entities,
        buttons=rows,
        media_kind=post.media_kind,
        media_file_id=post.media_file_id,
        pending=pending,
        prev_label=prev_label,
        new_label=rows[pending[0][0]][pending[0][1]]["t"],
        source_post_id=post.id,
    )


async def make_suggestion(db, channel_id: int) -> Optional[Suggestion]:
    for p in await db.latest_sent(channel_id, 30):
        s = build_suggestion(p)
        if s:
            return s
    return None
