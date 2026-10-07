"""Button model: rows of {'t': text, 'u': url} (URL buttons) or {'t', 'raw'} (other bots' buttons, kept as-is)."""
from __future__ import annotations

import re
from typing import Optional

MAX_ROW = 8
MAX_TOTAL = 100
_URL_RX = re.compile(r"(?:https?://|tg://|t\.me/|telegram\.me/)\S+", re.I)


def norm_url(u: str) -> Optional[str]:
    u = (u or "").strip()
    if re.match(r"(?i)^(t\.me|telegram\.me)/", u):
        u = "https://" + u
    if not re.match(r"(?i)^(https?://|tg://)\S+$", u) or len(u) > 2000:
        return None
    return u


def parse_buttons_text(text: str) -> list:
    """One row per line, 'Text - link'. Several buttons in a row: separate them with ' | '."""
    rows: list = []
    total = 0
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in re.split(r"\s+\|\s+|\s*&&\s*", line):
            part = part.strip()
            if not part:
                continue
            found = list(_URL_RX.finditer(part))
            if not found:
                raise ValueError(f"No link found in: {part}")
            m = found[-1]
            label = part[: m.start()].strip().rstrip("-–—:|=").strip()
            if not label:
                raise ValueError(f"No button text before the link in: {part}")
            url = norm_url(m.group(0))
            if not url:
                raise ValueError(f"That link doesn't look valid: {m.group(0)}")
            row.append({"t": label, "u": url})
        if len(row) > MAX_ROW:
            raise ValueError(f"At most {MAX_ROW} buttons fit in one row.")
        if row:
            rows.append(row)
            total += len(row)
    if total > MAX_TOTAL:
        raise ValueError(f"At most {MAX_TOTAL} buttons per post.")
    return rows


def copy_rows(rows: list) -> list:
    return [[dict(b) for b in row] for row in (rows or [])]


def count(rows: list) -> int:
    return sum(len(r) for r in rows or [])


def missing_links(rows: list) -> int:
    return sum(1 for r in rows or [] for b in r if not b.get("u") and not b.get("raw"))


def link_positions(rows: list) -> list:
    """(row, column) of every button that has a link of its own. Other bots' buttons (reactions...) are left out:
    they can't be edited."""
    return [(r, c) for r, row in enumerate(rows or []) for c, b in enumerate(row) if b.get("u") and not b.get("raw")]


def valid(rows: list, r: int, c: int) -> bool:
    return 0 <= r < len(rows) and 0 <= c < len(rows[r])


def add_button(rows: list, btn: dict, new_row: bool = True) -> list:
    rows = copy_rows(rows)
    if new_row or not rows or len(rows[-1]) >= MAX_ROW:
        rows.append([btn])
    else:
        rows[-1].append(btn)
    return rows


def set_field(rows: list, r: int, c: int, **fields) -> list:
    rows = copy_rows(rows)
    if valid(rows, r, c):
        rows[r][c].update(fields)
    return rows


def delete_button(rows: list, r: int, c: int) -> list:
    rows = copy_rows(rows)
    if valid(rows, r, c):
        rows[r].pop(c)
        rows = [row for row in rows if row]
    return rows


def move_button(rows: list, r: int, c: int, direction: str) -> tuple:
    """direction: l | r | u | d. Returns (rows, new_r, new_c)."""
    rows = copy_rows(rows)
    if not valid(rows, r, c):
        return rows, r, c
    b = rows[r][c]
    if direction == "l":
        if c > 0:
            rows[r][c - 1], rows[r][c] = rows[r][c], rows[r][c - 1]
            c -= 1
    elif direction == "r":
        if c < len(rows[r]) - 1:
            rows[r][c + 1], rows[r][c] = rows[r][c], rows[r][c + 1]
            c += 1
    elif direction == "u":
        if r > 0 and len(rows[r - 1]) < MAX_ROW:
            rows[r].pop(c)
            pos = min(c, len(rows[r - 1]))
            rows[r - 1].insert(pos, b)
            nr, nc = r - 1, pos
            if not rows[r]:
                rows.pop(r)
            r, c = nr, nc
    elif direction == "d":
        if r < len(rows) - 1:
            if len(rows[r + 1]) < MAX_ROW:
                rows[r].pop(c)
                pos = min(c, len(rows[r + 1]))
                rows[r + 1].insert(pos, b)
                nr, nc = r + 1, pos
                if not rows[r]:
                    rows.pop(r)
                    nr -= 1
                r, c = nr, nc
        elif len(rows[r]) > 1:  # last row with company: split off into a new row below
            rows[r].pop(c)
            rows.append([b])
            r, c = len(rows) - 1, 0
    return rows, r, c


def grid_label(b: dict) -> str:
    t = (b.get("t") or "").strip() or "…"
    if len(t) > 26:
        t = t[:25] + "…"
    if b.get("raw"):
        return "🔒 " + t
    if not b.get("u"):
        return "⚠️ " + t
    return t
