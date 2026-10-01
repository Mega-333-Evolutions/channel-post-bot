"""Texts and inline keyboards for the post panel."""
from __future__ import annotations

from telethon import Button

from .buttons import count, grid_label, missing_links
from .tgutil import esc, post_link, short

CANCEL = [[Button.inline("✖️ Cancel", "cx")]]
KIND_ICON = {None: "📝", "photo": "🖼", "video": "🎬", "animation": "🎞", "audio": "🎵", "document": "📎"}
KIND_NAME = {None: "text", "photo": "photo", "video": "video", "animation": "GIF", "audio": "audio", "document": "file"}


def post_title(p) -> str:
    for row in p.buttons or []:
        for b in row:
            if b.get("t"):
                return b["t"]
    text = (p.text or "").strip()
    return text.splitlines()[0] if text else (KIND_NAME.get(p.media_kind) or "post")


def post_label(p) -> str:
    icon = "📝" if p.status == "draft" else "✅"
    return f"{icon} #{p.id} {short(post_title(p), 34)}"


def panel_text(p, ch, note=None) -> str:
    state = "draft (not published yet)" if p.status == "draft" else "published"
    lines = [f"<b>Post #{p.id}</b> · {state}", f"Channel: <b>{esc(ch.title)}</b>"]
    if p.status == "sent" and p.message_id:
        lines.append(f'<a href="{post_link(ch, p.message_id)}">Open in the channel</a>')
    lines.append(f"Type: {KIND_ICON.get(p.media_kind, '📎')} {KIND_NAME.get(p.media_kind, p.media_kind)}")
    n, miss = count(p.buttons), missing_links(p.buttons)
    lines.append(f"Buttons: {n}" + (f" (⚠️ {miss} without a link)" if miss else ""))
    if p.source == "adopted":
        lines.append("<i>Picked up from the channel by /replace.</i>")
    snippet = short(p.text or "", 220) or "(no text)"
    lines += ["", f"<i>{esc(snippet)}</i>"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def panel_keyboard(p) -> list:
    pid = p.id
    kb = []
    for r, row in enumerate(p.buttons or []):
        kb.append([Button.inline(grid_label(b), f"bs:{pid}:{r}:{c}") for c, b in enumerate(row)])
    kb.append(
        [
            Button.inline("✏️ Caption" if p.media_kind else "✏️ Text", f"et:{pid}"),
            Button.inline("🖼 Media", f"em:{pid}"),
        ]
    )
    row3 = [Button.inline("➕ Button", f"ba:{pid}"), Button.inline("📋 Set buttons", f"bp:{pid}")]
    if p.buttons:
        row3.append(Button.inline("🧹 Clear", f"bc:{pid}"))
    kb.append(row3)
    extra = []
    if not p.media_kind:
        extra.append(Button.inline(f"🔗 Link preview: {'on' if p.link_preview else 'off'}", f"lp:{pid}"))
    if p.media_kind and p.status == "draft":
        extra.append(Button.inline("🚫 Remove media", f"rm:{pid}"))
    if extra:
        kb.append(extra)
    if p.status == "draft":
        kb.append([Button.inline("👁 Preview", f"pv:{pid}"), Button.inline("🚀 Publish", f"pb:{pid}")])
        kb.append([Button.inline("🗑 Discard", f"dl:{pid}"), Button.inline("📚 My posts", "pc:d:0")])
    else:
        kb.append([Button.inline("👁 Preview", f"pv:{pid}"), Button.inline("🗑 Delete", f"dl:{pid}")])
        kb.append([Button.inline("📚 My posts", f"pc:{p.channel_id}:0")])
    return kb


def button_text(p, r: int, c: int) -> str:
    b = p.buttons[r][c]
    if b.get("raw"):
        return (
            f"🔒 <b>Button from another bot</b> (row {r + 1}, position {c + 1})\n"
            f"Text: <code>{esc(b.get('t') or '')}</code>\n"
            "Buttons like reactions only work with the bot that made them, so you can move or delete it, "
            "but not edit it."
        )
    return (
        f"🔘 <b>Button</b> (row {r + 1}, position {c + 1})\n"
        f"Text: <code>{esc(b.get('t') or '')}</code>\n"
        f"Link: <code>{esc(b.get('u') or '- none yet -')}</code>"
    )


def button_keyboard(p, r: int, c: int) -> list:
    pid, at = p.id, f"{p.id}:{r}:{c}"
    b = p.buttons[r][c]
    kb = []
    if not b.get("raw"):
        kb.append([Button.inline("✏️ Change text", f"bt:{at}"), Button.inline("🔗 Change link", f"bu:{at}")])
    kb.append(
        [
            Button.inline("⬅️", f"bm:{at}:l"),
            Button.inline("⬆️", f"bm:{at}:u"),
            Button.inline("⬇️", f"bm:{at}:d"),
            Button.inline("➡️", f"bm:{at}:r"),
        ]
    )
    kb.append([Button.inline("🗑 Delete", f"bd:{at}"), Button.inline("🔙 Back", f"po:{pid}")])
    return kb
