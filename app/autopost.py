"""/autopost: the posts of a whole series - "Episodes 01 to 20", "Episodes 21 to 40", ... - each with a download button.

The ranges are planned here (pure functions, easy to test); the handler in handlers/autopost.py sends them.
"""
from __future__ import annotations

PLACEHOLDER_LINK = "https://t.me/English"  # every button starts with this link; the real ones are asked for afterwards
MAX_POSTS = 200  # the most one /autopost run may post
MAX_EPISODES = 100_000

USAGE = (
    "Usage: <code>/autopost &lt;total episodes&gt; &lt;interval&gt;</code>\n"
    "Example: <code>/autopost 114 20</code> posts Episodes 01 to 20, 21 to 40 ... up to 114, each with a download "
    "button, and then asks you for the real link of every button."
)


def parse_autopost_args(raw: str) -> tuple:
    """'114 20' -> (114, 20). ValueError (with a message to show) when it can't be used."""
    parts = (raw or "").replace(",", " ").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("I need two numbers: the total number of episodes and the interval.")
    total, interval = int(parts[0]), int(parts[1])
    if total < 1 or interval < 1:
        raise ValueError("Both numbers must be at least 1.")
    if total > MAX_EPISODES:
        raise ValueError(f"The total can be at most {MAX_EPISODES}.")
    return total, interval


def plan_ranges(total: int, interval: int) -> list:
    """[(first, last), ...] covering 1..total in steps of `interval`.

    The last, shorter range gets its own post when it is longer than half an interval (114 with 20 -> 101 to 114);
    when it is half an interval or less it is added to the post before it (102 with 20 -> 81 to 102)."""
    if total < 1 or interval < 1:
        raise ValueError("total and interval must be at least 1")
    ranges, start = [], 1
    while start <= total:
        end = min(start + interval - 1, total)
        ranges.append((start, end))
        start = end + 1
    if len(ranges) >= 2:
        a, b = ranges[-1]
        size = b - a + 1
        if size < interval and size * 2 <= interval:
            ranges.pop()
            ranges[-1] = (ranges[-1][0], b)
    return ranges


def pad(n: int) -> str:
    return f"{n:02d}"


def span(a: int, b: int) -> str:
    return f"{pad(a)} to {pad(b)}"


def title_of(a: int, b: int) -> str:
    return f"Episodes {span(a, b)}" if a != b else f"Episode {pad(a)}"


def post_text(a: int, b: int) -> str:
    return f"{title_of(a, b)}\n\nClick the button below to download 👇"


def button_label(a: int, b: int) -> str:
    return f"Download {title_of(a, b)}"


def post_buttons(a: int, b: int, link: str = PLACEHOLDER_LINK) -> list:
    return [[{"t": button_label(a, b), "u": link}]]


def ranges_summary(ranges: list, show: int = 8) -> str:
    """'01–20 · 21–40 · 41–60 · … · 101–114' (the middle is left out of long lists)."""
    parts = [f"{pad(a)}–{pad(b)}" if a != b else pad(a) for a, b in ranges]
    if len(parts) > show:
        parts = parts[: show - 2] + ["…"] + parts[-2:]
    return " · ".join(parts)
