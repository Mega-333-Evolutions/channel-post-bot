"""/posts: list, open and edit posts (text, caption, media, buttons), publish drafts, delete."""
from __future__ import annotations

import logging

from telethon import Button, errors

from ..buttons import (
    MAX_ROW,
    add_button,
    count,
    delete_button,
    missing_links,
    move_button,
    norm_url,
    parse_buttons_text,
    set_field,
    valid,
)
from ..common import (
    Ctx,
    UserError,
    apply_edit,
    clear_state,
    cmd,
    guard,
    on_cb,
    on_text,
    say,
    send_post,
    set_state,
    show,
)
from ..db import utcnow
from ..tgutil import classify_media, esc, explain_rpc, pack_file_id, peer_of, post_link, ser_entities, short
from ..ui import CANCEL, post_label
from .panel import (
    check_lengths,
    load_post,
    show_button_screen,
    show_panel,
)

log = logging.getLogger(__name__)
PAGE = 8
UPDATED = "✅ Updated in the channel."


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    def note_for(post) -> str:
        return UPDATED if post.status == "sent" else "✅ Saved."

    # ------------------------------------------------------------------ lists
    async def channel_chooser(event) -> None:
        chans = await db.list_channels()
        kb = [[Button.inline(f"📢 {short(c.title, 40)}", f"pc:{c.id}:0")] for c in chans]
        kb.append([Button.inline("💾 Drafts (all channels)", "pc:d:0")])
        await show(event, "📚 <b>My posts</b> - pick a channel:", kb)

    @client.on(cmd("posts"))
    @guard(ctx)
    async def h_posts(event):
        await channel_chooser(event)

    @on_cb(ctx, "pl")
    async def cb_channel_chooser(event, parts):
        await channel_chooser(event)

    @on_cb(ctx, "pc")
    async def cb_list(event, parts):
        key, page = parts[0], int(parts[1])
        if key == "d":
            cid, status, title = None, "draft", "Drafts"
        else:
            cid, status = int(key), None
            ch = await db.get_channel(cid)
            title = ch.title if ch else "Channel"
        rows, total = await db.list_posts(cid, status, page * PAGE, PAGE)
        pages = max(1, -(-total // PAGE))
        if page >= pages:
            page = pages - 1
            rows, total = await db.list_posts(cid, status, page * PAGE, PAGE)
        kb = [[Button.inline(post_label(p), f"po:{p.id}")] for p in rows]
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️ Prev", f"pc:{key}:{page - 1}"))
            nav.append(Button.inline(f"{page + 1}/{pages}", "noop"))
            if page < pages - 1:
                nav.append(Button.inline("Next ➡️", f"pc:{key}:{page + 1}"))
            kb.append(nav)
        kb.append([Button.inline("🔙 Channels", "pl")])
        head = f"📚 <b>{esc(title)}</b> - {total} post(s)"
        if not rows:
            head += "\n\nNothing here yet. Create a post with /new."
        await show(event, head, kb)

    @on_cb(ctx, "po")
    async def cb_open(event, parts):
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, parts[0])

    # ------------------------------------------------------------ text / caption
    @on_cb(ctx, "et")
    async def cb_edit_text(event, parts):
        post, _ = await load_post(ctx, parts[0])
        set_state(ctx, event.sender_id, "edit_text", pid=post.id)
        what = "caption" if post.media_kind else "text"
        await say(event, f"✏️ Send the new {what}. Formatting is kept.", CANCEL)

    @on_text(ctx, "edit_text")
    async def h_edit_text(event, st):
        m = event.message
        if classify_media(m):
            raise UserError("Send text only here. To change the media, use 🖼 Media.")
        post, ch = await load_post(ctx, st["pid"])
        text = m.raw_text or ""
        if not post.media_kind and not text.strip():
            raise UserError("A text post can't be empty.")
        check_lengths(text, bool(post.media_kind))
        post = await apply_edit(ctx, post, ch, text=text, entities=ser_entities(m.entities))
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, post.id, note=note_for(post), new=True)

    # ------------------------------------------------------------------- media
    @on_cb(ctx, "em")
    async def cb_edit_media(event, parts):
        post, _ = await load_post(ctx, parts[0])
        set_state(ctx, event.sender_id, "edit_media", pid=post.id)
        extra = "" if post.media_kind else "\nThe current text becomes its caption."
        await say(event, f"🖼 Send the new photo / video / GIF / audio / file.{extra}", CANCEL)

    @on_text(ctx, "edit_media")
    async def h_edit_media(event, st):
        m = event.message
        if m.grouped_id and st.get("group") == m.grouped_id:
            return
        if m.grouped_id:
            st["group"] = m.grouped_id
        kind = classify_media(m)
        if not kind:
            raise UserError("Send a photo, video, GIF, audio file or document.")
        post, ch = await load_post(ctx, st["pid"])
        check_lengths(post.text or "", True)
        post = await apply_edit(ctx, post, ch, replace_media=True, media_kind=kind, media_file_id=pack_file_id(m))
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, post.id, note=note_for(post), new=True)

    @on_cb(ctx, "rm")
    async def cb_remove_media(event, parts):
        post, ch = await load_post(ctx, parts[0])
        if post.status != "draft":
            raise UserError("Media can't be removed from a published post - Telegram doesn't allow it.")
        if not (post.text or "").strip():
            raise UserError("Add some text first, then remove the media (a post needs text or media).")
        await db.update_post(post.id, media_kind=None, media_file_id=None)
        await show_panel(ctx, event, post.id, note="✅ Media removed.")

    @on_cb(ctx, "lp")
    async def cb_link_preview(event, parts):
        post, ch = await load_post(ctx, parts[0])
        post = await apply_edit(ctx, post, ch, link_preview=not post.link_preview)
        await show_panel(ctx, event, post.id)

    # ----------------------------------------------------------------- buttons
    async def commit_buttons(event, post, ch, rows, *, new_msg: bool = True, note: str = None):
        post = await apply_edit(ctx, post, ch, buttons=rows)
        await show_panel(ctx, event, post.id, note=note or note_for(post), new=new_msg)
        return post

    @on_cb(ctx, "ba")
    async def cb_add_button(event, parts):
        post, _ = await load_post(ctx, parts[0])
        if count(post.buttons) >= 100:
            raise UserError("A post can have at most 100 buttons.")
        set_state(ctx, event.sender_id, "btn_add_text", pid=post.id)
        await say(
            event,
            "➕ Send the button as <code>Button text - link</code>, or just the button text and I'll ask for the link.",
            CANCEL,
        )

    async def place_button(event, pid, btn: dict) -> None:
        post, ch = await load_post(ctx, pid)
        if post.buttons and len(post.buttons[-1]) < MAX_ROW:
            set_state(ctx, event.sender_id, "btn_place", pid=post.id, btn=btn)
            await say(
                event,
                f"Where should <code>{esc(btn['t'])}</code> go?",
                [
                    [Button.inline("⬇️ New row", f"bk:{post.id}:n"), Button.inline("➡️ Same row", f"bk:{post.id}:s")],
                    CANCEL[0],
                ],
            )
            return
        clear_state(ctx, event.sender_id)
        await commit_buttons(event, post, ch, add_button(post.buttons, btn, True))

    @on_text(ctx, "btn_add_text")
    async def h_btn_add_text(event, st):
        txt = (event.raw_text or "").strip()
        if not txt:
            raise UserError("Send the button text.")
        try:
            rows = parse_buttons_text(txt)
        except ValueError:
            rows = []
        if rows:
            if len(rows) == 1 and len(rows[0]) == 1:
                await place_button(event, st["pid"], rows[0][0])
                return
            raise UserError("That's several buttons. Send one at a time, or use 📋 Set buttons to replace them all.")
        if len(txt) > 64:
            raise UserError("Keep the button text under 64 characters.")
        set_state(ctx, event.sender_id, "btn_add_url", pid=st["pid"], text=txt)
        await say(event, f"🔗 Now send the link for <code>{esc(txt)}</code>.", CANCEL)

    @on_text(ctx, "btn_add_url")
    async def h_btn_add_url(event, st):
        url = norm_url(event.raw_text or "")
        if not url:
            raise UserError("That doesn't look like a link. Send something like https://t.me/yourbot?start=abc")
        await place_button(event, st["pid"], {"t": st["text"], "u": url})

    @on_cb(ctx, "bk")
    async def cb_place(event, parts):
        st = ctx.state.get(event.sender_id)
        if not st or st.get("mode") != "btn_place" or str(st.get("pid")) != parts[0]:
            raise UserError("That step expired. Tap ➕ Button again.")
        post, ch = await load_post(ctx, parts[0])
        rows = add_button(post.buttons, st["btn"], new_row=(parts[1] == "n"))
        clear_state(ctx, event.sender_id)
        await commit_buttons(event, post, ch, rows, new_msg=False)

    @on_cb(ctx, "bp")
    async def cb_paste(event, parts):
        post, _ = await load_post(ctx, parts[0])
        set_state(ctx, event.sender_id, "btn_paste", pid=post.id)
        warn = ""
        if any(b.get("raw") for r in post.buttons for b in r):
            warn = "\n⚠️ This also removes buttons made by other bots (like reactions)."
        await say(
            event,
            "📋 Send <b>all</b> the buttons - one row per line as <code>Text - link</code>, "
            "with <code> | </code> between buttons in one row. This replaces the current buttons." + warn,
            CANCEL,
        )

    @on_text(ctx, "btn_paste")
    async def h_btn_paste(event, st):
        try:
            rows = parse_buttons_text(event.raw_text or "")
        except ValueError as e:
            raise UserError(f"❌ {e}\n\nTry again.")
        if not rows:
            raise UserError("Send at least one button as “Text - link”.")
        post, ch = await load_post(ctx, st["pid"])
        clear_state(ctx, event.sender_id)
        await commit_buttons(event, post, ch, rows)

    @on_cb(ctx, "bc")
    async def cb_clear(event, parts):
        await show(
            event,
            "Remove <b>all</b> buttons from this post?",
            [[Button.inline("🧹 Yes, remove", f"bcy:{parts[0]}"), Button.inline("↩️ Back", f"po:{parts[0]}")]],
        )

    @on_cb(ctx, "bcy")
    async def cb_clear_yes(event, parts):
        post, ch = await load_post(ctx, parts[0])
        await commit_buttons(event, post, ch, [], new_msg=False)

    @on_cb(ctx, "bs")
    async def cb_select(event, parts):
        clear_state(ctx, event.sender_id)
        await show_button_screen(ctx, event, parts[0], int(parts[1]), int(parts[2]))

    @on_cb(ctx, "bt")
    async def cb_btn_text(event, parts):
        set_state(ctx, event.sender_id, "btn_text", pid=parts[0], r=int(parts[1]), c=int(parts[2]))
        await say(event, "✏️ Send the new button text.", CANCEL)

    @on_text(ctx, "btn_text")
    async def h_btn_text(event, st):
        txt = (event.raw_text or "").strip()
        if not txt:
            raise UserError("Send the new button text.")
        if len(txt) > 64:
            raise UserError("Keep the button text under 64 characters.")
        post, ch = await load_post(ctx, st["pid"])
        if not valid(post.buttons, st["r"], st["c"]):
            raise UserError("That button no longer exists.")
        post = await apply_edit(ctx, post, ch, buttons=set_field(post.buttons, st["r"], st["c"], t=txt))
        clear_state(ctx, event.sender_id)
        await show_button_screen(ctx, event, post.id, st["r"], st["c"], new=True)

    @on_cb(ctx, "bu")
    async def cb_btn_url(event, parts):
        set_state(ctx, event.sender_id, "btn_url", pid=parts[0], r=int(parts[1]), c=int(parts[2]))
        await say(event, "🔗 Send the new link for this button.", CANCEL)

    @on_text(ctx, "btn_url")
    async def h_btn_url(event, st):
        url = norm_url(event.raw_text or "")
        if not url:
            raise UserError("That doesn't look like a link. Send something like https://t.me/yourbot?start=abc")
        post, ch = await load_post(ctx, st["pid"])
        if not valid(post.buttons, st["r"], st["c"]):
            raise UserError("That button no longer exists.")
        post = await apply_edit(ctx, post, ch, buttons=set_field(post.buttons, st["r"], st["c"], u=url))
        clear_state(ctx, event.sender_id)
        await show_button_screen(ctx, event, post.id, st["r"], st["c"], new=True)

    @on_cb(ctx, "bm")
    async def cb_move(event, parts):
        pid, r, c, d = parts[0], int(parts[1]), int(parts[2]), parts[3]
        post, ch = await load_post(ctx, pid)
        rows, nr, nc = move_button(post.buttons, r, c, d)
        if rows == post.buttons:
            return  # already at the edge
        post = await apply_edit(ctx, post, ch, buttons=rows)
        await show_button_screen(ctx, event, post.id, nr, nc)

    @on_cb(ctx, "bd")
    async def cb_btn_delete(event, parts):
        post, ch = await load_post(ctx, parts[0])
        rows = delete_button(post.buttons, int(parts[1]), int(parts[2]))
        await commit_buttons(event, post, ch, rows, new_msg=False)

    # ------------------------------------------------------- preview / publish
    @on_cb(ctx, "pv")
    async def cb_preview(event, parts):
        post, _ = await load_post(ctx, parts[0])
        if not (post.text or "").strip() and not post.media_file_id:
            raise UserError("The post is empty.")
        await send_post(client, await event.get_input_chat(), post)
        miss = missing_links(post.buttons)
        if miss:
            await say(event, f"👆 Preview. ⚠️ {miss} button(s) without a link are not shown.")

    @on_cb(ctx, "pb")
    async def cb_publish(event, parts):
        post, ch = await load_post(ctx, parts[0])
        if post.status != "draft":
            raise UserError("This post is already published.")
        if missing_links(post.buttons):
            raise UserError("Some buttons (⚠️) have no link yet. Open them and add a link first.")
        if not (post.text or "").strip() and not post.media_file_id:
            raise UserError("The post is empty.")
        await show(
            event,
            f"Publish this post to <b>{esc(ch.title)}</b>?",
            [[Button.inline("🚀 Yes, publish", f"pby:{post.id}"), Button.inline("↩️ Back", f"po:{post.id}")]],
        )

    @on_cb(ctx, "pby")
    async def cb_publish_yes(event, parts):
        post, ch = await load_post(ctx, parts[0])
        if post.status != "draft":
            raise UserError("This post is already published.")
        try:
            sent = await send_post(client, peer_of(ch), post)
        except errors.RPCError as e:
            raise UserError("❌ " + explain_rpc(e) + "\nThe post was NOT published.")
        post = await db.update_post(post.id, status="sent", message_id=sent.id, sent_at=utcnow())
        await show(
            event,
            f"✅ <b>Published</b> in <b>{esc(ch.title)}</b>\n<a href=\"{post_link(ch, sent.id)}\">Open the post</a>",
            [
                [Button.inline("📝 Manage this post", f"po:{post.id}")],
                [Button.inline("➕ New post", f"nc:{ch.id}")],
            ],
        )

    # ----------------------------------------------------------------- delete
    @on_cb(ctx, "dl")
    async def cb_delete(event, parts):
        post, ch = await load_post(ctx, parts[0])
        back = Button.inline("↩️ Back", f"po:{post.id}")
        if post.status == "draft":
            await show(event, "Discard this draft?", [[Button.inline("🗑 Yes, discard", f"dly:{post.id}:b"), back]])
            return
        await show(
            event,
            f"Delete this post from <b>{esc(ch.title)}</b>?\n\n"
            "Telegram's documentation says bots can only delete posts younger than 48 hours; in channels that limit may "
            "not apply. If Telegram refuses, delete the post by hand and then use “Only forget it in the bot”.",
            [
                [Button.inline("🗑 Delete from channel", f"dly:{post.id}:a")],
                [Button.inline("🧹 Only forget it in the bot", f"dly:{post.id}:b")],
                [back],
            ],
        )

    @on_cb(ctx, "dly")
    async def cb_delete_yes(event, parts):
        post, ch = await load_post(ctx, parts[0])
        if post.status == "sent" and parts[1] == "a":
            res = await client.delete_messages(peer_of(ch), [post.message_id])
            if not sum(getattr(r, "pts_count", 0) or 0 for r in res):
                raise UserError(
                    "Telegram did not delete that message (the bot may lack the Delete right, or the post is older than "
                    "the 48 hours Telegram's documentation mentions). Delete it by hand in the channel, then use "
                    "“Only forget it in the bot”."
                )
        await db.delete_post(post.id)
        await show(event, "🗑 Done.", [[Button.inline("📚 My posts", "pl")]])
