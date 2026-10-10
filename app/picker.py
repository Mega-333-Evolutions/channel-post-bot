"""The channel chooser used by every command that needs one of the connected channels.

The names are written out in the message as a numbered list (A to Z, 20 to a page); the buttons only carry the numbers
(5 to a row), and ◀ ▶ underneath move to the previous / next 20. Pressing "3" means the third name of the list.
Each button carries the channel's id, never its position, so a list that changed meanwhile can't pick the wrong one.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Optional, Sequence

from telethon import Button

from .tgutil import esc, short

PER_PAGE = 20  # channel names in one message
PER_ROW = 5  # number buttons in one row
NAME_MAX = 60  # a longer name is cut in the list
PAGE_CB = "cpg"  # callback prefix of the ◀ ▶ buttons: cpg:<screen>:<argument>:<page>

_DIGITS = re.compile(r"(\d+)")


def natural_key(title: Optional[str]) -> tuple:
    """A to Z, numbers by value ("Anime 2" before "Anime 10"), upper and lower case alike."""
    parts = _DIGITS.split(" ".join((title or "").split()).casefold())
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p != "")


def ordered(chans: Sequence) -> list:
    """The channels in the order they are listed: by name, ascending (the id breaks ties)."""
    return sorted(chans, key=lambda c: (natural_key(c.title), c.id))


def page_count(total: int) -> int:
    return max(1, -(-total // PER_PAGE))


def clamp(page: int, total: int) -> int:
    return max(0, min(int(page), page_count(total) - 1))


def names_of(chans: Sequence, *, with_username: bool = False) -> list:
    """Display names (not yet escaped). Two channels with the same name get their @username (or id) added."""
    keys = [" ".join((c.title or "").split()).casefold() for c in chans]
    twins = Counter(keys)
    out = []
    for c, key in zip(chans, keys):
        name = short(c.title or "", NAME_MAX) or "Channel"
        if with_username and c.username:
            name += f" (@{c.username})"
        elif twins[key] > 1:
            name += f" (@{c.username})" if c.username else f" ({c.id})"
        out.append(name)
    return out


def render(
    chans: Sequence,
    page: int,
    *,
    head: str,
    kind: str,
    arg: str = "",
    choose: Optional[Callable] = None,
    extra_rows: Sequence = (),
    with_username: bool = False,
    hint: Optional[str] = "Tap the number of the channel.",
) -> tuple:
    """(text, button rows) of one page.

    head       the question, already HTML
    kind, arg  which screen this is - the ◀ ▶ buttons carry them so the same screen can be drawn again
    choose     channel -> callback data of its number button (None: a list to read, no number buttons)
    extra_rows buttons under the arrows (Cancel ...)
    hint       the line under the list that says what the number buttons do (None: leave it out)
    """
    chans = ordered(chans)
    total = len(chans)
    pages = page_count(total)
    page = clamp(page, total)
    first = page * PER_PAGE
    shown = chans[first : first + PER_PAGE]
    names = names_of(chans, with_username=with_username)[first : first + PER_PAGE]
    lines = [f"{first + i}. {esc(name)}" for i, name in enumerate(names, 1)]
    text = f"{head}\n\n" + "\n".join(lines)
    if pages > 1:
        text += f"\n\nPage {page + 1} of {pages} - channels {first + 1} to {first + len(shown)} of {total}"
    if choose is not None and hint:
        text += f"\n\n{hint}"
    rows: list = []
    if choose is not None:
        nums = [Button.inline(str(first + i), choose(c)) for i, c in enumerate(shown, 1)]
        rows += [nums[i : i + PER_ROW] for i in range(0, len(nums), PER_ROW)]
    if pages > 1:  # always both arrows: on the first page ◀ goes to the last page, on the last page ▶ to the first
        rows.append(
            [
                Button.inline("◀", f"{PAGE_CB}:{kind}:{arg}:{(page - 1) % pages}"),
                Button.inline("▶", f"{PAGE_CB}:{kind}:{arg}:{(page + 1) % pages}"),
            ]
        )
    rows += [list(r) for r in extra_rows]
    return text, rows
