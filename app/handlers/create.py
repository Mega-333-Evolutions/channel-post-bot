"""/new: create a post, with the episode-range auto-suggestion."""
from __future__ import annotations

import logging

from telethon import Button

from .. import picker
from ..buttons import norm_url, parse_buttons_text, set_field
from ..common import Ctx, UserError, clear_state, cmd, discard_temp, guard, on_cb, on_text, say, set_state, show
from ..suggest import make_suggestion
from ..tgutil import esc, short
from ..ui import CANCEL, KIND_NAME
from .panel import extract_content, show_panel

log = logging.getLogger(__name__)

BUTTON_HELP = (
    "➕ <b>Now the buttons</b>\n"
    "Send one row per line as <code>Button text - link</code>.\n"
    "Several buttons in one row: separate them with <code> | </code>.\n\n"
    "Example:\n<code>Download Episodes 01 to 20 - https://t.me/yourbot?start=abc</code>\n\n"
    "Or send /skip to post without buttons."
)


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    async def ask_content(event, cid: int) -> None:
        set_state(ctx, event.sender_id, "new_content", cid=cid)
        await show(
            event,
            "✍️ <b>Send the post now.</b>\nText, or a photo / video / GIF / file with a caption. "
            "Formatting (bold, links, spoilers...) is kept.",
            CANCEL,
        )

    async def start_for_channel(event, cid: int) -> None:
        ch = await db.get_channel(cid)
        if ch is None:
            raise UserError("That channel is not registered. Use /addchannel.")
        sug = await make_suggestion(db, cid)
        if sug is None:
            await ask_content(event, cid)
            return
        lines = [
            f"💡 <b>Suggestion for {esc(ch.title)}</b>",
            f"Your last button post: <code>{esc(sug.prev_label)}</code>",
            "The next one would be:",
        ]
        if (sug.text or "").strip():
            lines.append(f"• Message: <i>{esc(short(sug.text, 120))}</i>")
        lines.append(f"• Button: <code>{esc(sug.new_label)}</code>")
        if sug.media_kind:
            lines.append(f"• Media: same {KIND_NAME.get(sug.media_kind, 'file')} as that post (you can change it before publishing)")
        lines.append("\nUse it and I'll only ask for the button link.")
        kb = [
            [Button.inline("✅ Use suggestion", f"ns:{cid}"), Button.inline("✏️ Customize", f"nn:{cid}")],
            CANCEL[0],
        ]
        await show(event, "\n".join(lines), kb)

    async def ask_link(event, post, pos) -> None:
        r, c = pos
        label = post.buttons[r][c]["t"]
        await say(
            event,
            f"🔗 Send the link for the button <code>{esc(label)}</code>\n"
            "(for example <code>https://t.me/yourbot?start=abc</code>)",
            CANCEL,
        )

    @client.on(cmd("new"))
    @guard(ctx)
    async def h_new(event):
        uid = event.sender_id
        await discard_temp(ctx, uid)
        clear_state(ctx, uid)
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels yet. Register one with /addchannel first.")
        if len(chans) == 1:
            await start_for_channel(event, chans[0].id)
            return
        await show_picker(event, "", 0)

    async def show_picker(event, arg: str, page: int) -> None:
        chans = await db.list_channels()
        if not chans:
            raise UserError("No channels yet. Register one with /addchannel first.")
        text, kb = picker.render(
            chans, page, head="📢 <b>Which channel is this post for?</b>", kind="nc", choose=lambda c: f"nc:{c.id}", extra_rows=CANCEL
        )
        await show(event, text, kb)

    ctx.pickers["nc"] = show_picker

    @on_cb(ctx, "nc")
    async def cb_channel(event, parts):
        await discard_temp(ctx, event.sender_id)
        await start_for_channel(event, int(parts[0]))

    @on_cb(ctx, "nn")
    async def cb_customize(event, parts):
        await ask_content(event, int(parts[0]))

    @on_cb(ctx, "ns")
    async def cb_use_suggestion(event, parts):
        uid, cid = event.sender_id, int(parts[0])
        sug = await make_suggestion(db, cid)
        if sug is None:
            raise UserError("There is nothing to suggest any more - send the post yourself with /new.")
        await discard_temp(ctx, uid)
        post = await db.create_post(
            channel_id=cid,
            created_by=uid,
            status="draft",
            source="bot",
            text=sug.text,
            entities=sug.entities,
            buttons=sug.buttons,
            media_kind=sug.media_kind,
            media_file_id=sug.media_file_id,
            link_preview=False,
        )
        set_state(ctx, uid, "new_link", pid=post.id, pending=list(sug.pending), temp=True)
        await show(event, f"✅ Using the suggestion: <code>{esc(sug.new_label)}</code>")
        await ask_link(event, post, sug.pending[0])

    @on_text(ctx, "new_link")
    async def h_new_link(event, st):
        url = norm_url(event.raw_text or "")
        if not url:
            raise UserError("That doesn't look like a link. Send something like https://t.me/yourbot?start=abc")
        post = await db.get_post(st["pid"])
        if post is None:
            clear_state(ctx, event.sender_id)
            raise UserError("That draft is gone. Start again with /new.")
        r, c = st["pending"][0]
        post = await db.update_post(post.id, buttons=set_field(post.buttons, r, c, u=url))
        st["pending"] = st["pending"][1:]
        if st["pending"]:
            await ask_link(event, post, st["pending"][0])
            return
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, post.id, note="✅ Ready. Preview it, or tap <b>Publish</b>.", new=True)

    @on_text(ctx, "new_content")
    async def h_new_content(event, st):
        m = event.message
        if m.grouped_id and st.get("group") == m.grouped_id:
            return  # the other items of an album: only the first one is used
        if m.grouped_id:
            st["group"] = m.grouped_id
        content = extract_content(m)
        post = await db.create_post(
            channel_id=st["cid"],
            created_by=event.sender_id,
            status="draft",
            source="bot",
            buttons=[],
            link_preview=False,
            **content,
        )
        set_state(ctx, event.sender_id, "new_buttons", pid=post.id, temp=True)
        await say(event, BUTTON_HELP, CANCEL)

    @on_text(ctx, "new_buttons")
    async def h_new_buttons(event, st):
        if event.message.grouped_id:
            return
        try:
            rows = parse_buttons_text(event.raw_text or "")
        except ValueError as e:
            raise UserError(f"❌ {e}\n\nTry again, or send /skip.")
        if not rows:
            raise UserError("Send at least one button as “Text - link”, or /skip.")
        await db.update_post(st["pid"], buttons=rows)
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, st["pid"], note="✅ Ready. Preview it, or tap <b>Publish</b>.", new=True)

    @client.on(cmd("skip"))
    @guard(ctx)
    async def h_skip(event):
        st = ctx.state.get(event.sender_id)
        if not st or st["mode"] != "new_buttons":
            raise UserError("There is nothing to skip right now.")
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, st["pid"], note="✅ Ready (no buttons). Preview it, or tap <b>Publish</b>.", new=True)
