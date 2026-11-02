"""/posts: list, open and edit posts (text, caption, media, buttons), publish drafts, delete."""
from __future__ import annotations

import logging

from telethon import Button, errors

from .. import picker
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
    apply_edit_reported,
    clear_state,
    cmd,
    edit_report_text,
    guard,
    on_cb,
    on_text,
    say,
    send_post,
    set_state,
    show,
)
from ..db import utcnow
from ..repost_engine import delete_batch
from ..richmsg import (
    block_count,
    block_labels,
    digest,
    entry as rich_entry,
    is_block_invalid,
    is_rich,
    pack_blocks,
    plain_of,
    refused_blocks,
    rich_of,
    unpack_blocks,
    without_blocks,
)
from ..tgutil import classify_media, esc, explain_rpc, pack_file_id, peer_of, post_link
from ..ui import CANCEL, post_label
from ..userbot import delete_problem_text
from .panel import (
    check_lengths,
    is_rich_how,
    load_post,
    markup_note,
    read_native,
    read_text,
    show_button_screen,
    show_panel,
)

log = logging.getLogger(__name__)
PAGE = 8
UPDATED = "✅ Updated in the channel."


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    def note_for(post, report=None) -> str:
        extra = edit_report_text(report)
        return (UPDATED if post.status == "sent" else "✅ Saved.") + (f"\n{extra}" if extra else "")

    # ------------------------------------------------------------------ lists
    async def channel_chooser(event, arg: str = "", page: int = 0) -> None:
        chans = await db.list_channels()
        drafts = [[Button.inline("💾 Drafts (all channels)", "pc:d:0")]]
        if not chans:
            await show(event, "📚 <b>My posts</b>\n\nNo channels yet. Add one with /addchannel.", drafts)
            return
        text, kb = picker.render(
            chans, page, head="📚 <b>My posts</b> - pick a channel:", kind="ps", choose=lambda c: f"pc:{c.id}:0", extra_rows=drafts
        )
        await show(event, text, kb)

    ctx.pickers["ps"] = channel_chooser

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
            # "Prev" is always there: on the first page it jumps to the last one
            nav = [Button.inline("⬅️ Prev", f"pc:{key}:{(page - 1) % pages}"), Button.inline(f"{page + 1}/{pages}", "noop")]
            if page < pages - 1:
                nav.append(Button.inline("Next ➡️", f"pc:{key}:{page + 1}"))
            kb.append(nav)
        if cid is not None and ctx.cfg.is_owner(event.sender_id):
            kb.append([Button.inline("🔧 Check buttons", f"pk:{cid}")])
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
        more = "" if post.media_kind else " Rich messages (headings, lists, tables ...) too: /rich."
        await say(event, f"✏️ Send the new {what}. Formatting is kept - or type HTML / Markdown (/formatting shows how).{more}", CANCEL)

    @on_text(ctx, "edit_text")
    async def h_edit_text(event, st):
        m = event.message
        if classify_media(m):
            raise UserError("Send text only here. To change the media, use 🖼 Media.")
        post, ch = await load_post(ctx, st["pid"])
        native = await read_native(ctx, event)
        text, entities, how = read_text(m, ctx.cfg.markup_input, native, allow_rich=not post.media_kind)
        if not post.media_kind and not text.strip() and not is_rich_how(how):
            raise UserError("A text post can't be empty.")
        if not is_rich_how(how):
            check_lengths(text, bool(post.media_kind))
        post, report = await apply_edit_reported(ctx, post, ch, text=text, entities=entities)
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, post.id, note=markup_note(how, "\n") + note_for(post, report), new=True)

    # ------------------------------------------------------------------- media
    @on_cb(ctx, "em")
    async def cb_edit_media(event, parts):
        post, _ = await load_post(ctx, parts[0])
        if is_rich(post):
            raise UserError(
                "A rich message has no media slot. Put pictures into the text itself (see /rich), "
                "or send new text without rich blocks first."
            )
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

        async def notify(text: str) -> None:
            await say(event, text)

        post, report = await apply_edit_reported(
            ctx, post, ch, replace_media=True, notify=notify, media_kind=kind, media_file_id=pack_file_id(m)
        )
        clear_state(ctx, event.sender_id)
        await show_panel(ctx, event, post.id, note=note_for(post, report), new=True)

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
    async def rich_refusal(event, post, e, typed: str, tail: str = "") -> None:
        """Telegram refused to make a rich message of the post: say why.

        A rich message that was typed (HTML / Markdown) is Telegram's to read: `typed` tells the owner to check the syntax. One
        that was made in Telegram and sent to the bot is kept block by block, and Telegram does not say which block it
        dislikes - so the blocks are tried out, in the owner's chat and quietly, to tell which ones it won't take (see
        richmsg.refused_blocks). A draft can then go on without those: the ✂️ button. `tail` closes the message."""
        rich = rich_of(post)
        head = "❌ " + explain_rpc(e)
        if rich is None or rich.fmt != "blocks":
            raise UserError(f"{head}\n{typed}")
        if not is_block_invalid(e):
            raise UserError(f"{head}\nTelegram could not make that rich message.\n{tail}".strip())
        bad = await refused_blocks(client, await event.get_input_chat(), rich)
        if not bad:
            raise UserError(
                f"{head}\nI could not tell which block it is - /rich shows how to type the message as HTML / Markdown instead.\n{tail}".strip()
            )
        if len(bad) >= block_count(rich):
            raise UserError(
                f"{head}\nIt won't take any block of that message as it is - /rich shows how to type it as HTML / Markdown instead.\n{tail}".strip()
            )
        lines = [
            esc(head),
            f"It won't take these blocks (of {block_count(rich)}): {esc(block_labels(rich, bad))}. The others are fine.",
        ]
        if tail:
            lines.append(esc(tail))
        buttons = []
        if post.status == "draft":
            lines.append("✂️ <b>Leave those out</b> keeps the rest of the message as the post.")
            buttons.append([Button.inline("✂️ Leave those out", f"rx:{post.id}:{digest(rich)}:{pack_blocks(bad)}")])
        buttons.append([Button.inline("↩️ Back", f"po:{post.id}")])
        await say(event, "\n".join(lines), buttons)

    @on_cb(ctx, "rx")
    async def cb_leave_out(event, parts):
        post, ch = await load_post(ctx, parts[0])
        rich = rich_of(post)
        if post.status != "draft" or rich is None or rich.fmt != "blocks" or parts[1] != digest(rich):
            raise UserError("The post is not the one that was checked any more - press 👁 Preview again.")
        total = block_count(rich)
        drop = [i for i in unpack_blocks(parts[2]) if i < total]
        if not drop:
            raise UserError("Those blocks are not in the post any more.")
        if len(drop) >= total:
            raise UserError("Nothing would be left of the message. Send it again in another form, or as HTML / Markdown (/rich).")
        kept = without_blocks(rich, drop)
        post = await apply_edit(ctx, post, ch, text=plain_of(kept), entities=[rich_entry(kept)])
        await show_panel(ctx, event, post.id, note=f"✂️ Left out: {esc(block_labels(rich, drop))}.\n👁 Preview shows what is left.")

    @on_cb(ctx, "pv")
    async def cb_preview(event, parts):
        post, _ = await load_post(ctx, parts[0])
        if not (post.text or "").strip() and not post.media_file_id and not is_rich(post):
            raise UserError("The post is empty.")
        try:
            await send_post(client, await event.get_input_chat(), post)
        except errors.RPCError as e:
            if not is_rich(post):
                raise
            await rich_refusal(event, post, e, typed="Telegram could not make that rich message - see /rich for the syntax.")
            return
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
        if not (post.text or "").strip() and not post.media_file_id and not is_rich(post):
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
            if is_rich(post):
                await rich_refusal(event, post, e, typed="The post was NOT published.", tail="The post was NOT published.")
                return
            raise UserError("❌ " + explain_rpc(e) + "\nThe post was NOT published.")
        await db.release_slot(ch.id, sent.id, keep_pid=post.id)  # the sync may have picked the message up a moment ago
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
            "🗑 <b>Delete from channel</b> - deletes it in the channel and removes it, with its data, from My posts.\n"
            "🧹 <b>Only forget it in the bot</b> - the post stays in the channel; only My posts forgets it.",
            [
                [Button.inline("🗑 Delete from channel", f"dly:{post.id}:a")],
                [Button.inline("🧹 Only forget it in the bot", f"dly:{post.id}:b")],
                [back],
            ],
        )

    @on_cb(ctx, "dly")
    async def cb_delete_yes(event, parts):
        post, ch = await load_post(ctx, parts[0])
        note = "🗑 Done."
        if post.status == "sent" and parts[1] == "a":
            # The bot deletes the post; where Telegram refuses it (an age limit on old posts, a missing right) the userbot
            # takes over at once - nothing is asked first.
            fallback, deleter = await ctx.userbot.fallback_for(ch) if ctx.userbot is not None else (None, None)
            try:
                out = await delete_batch(client, peer_of(ch), [post.message_id], fallback)
            finally:
                if deleter is not None:
                    await deleter.close()
            if post.message_id not in out.gone:
                why = delete_problem_text(
                    ctx.userbot, bot_error=out.bot_error, fallback_error=out.fallback_error, tried=out.tried_userbot
                )
                raise UserError(
                    f"Telegram did not delete that message. {why}\n"
                    "The post is still in My posts. Delete it by hand in the channel, then use “Only forget it in the bot”."
                )
            # gone from the channel: it goes from My posts too, with what hangs on it (a timed deletion ...)
            await db.forget_posts(post.channel_id, [post.message_id])
            note = "🗑 Done - deleted in the channel and removed from My posts."
            if out.by_userbot:
                note += "\nThe bot was not allowed to delete it, so the userbot did."
        if post.status == "sent" and post.message_id and parts[1] == "b":
            ctx.sync_ignore.add((post.channel_id, post.message_id))  # forgotten on purpose: the sync must not bring it back
            await db.ignore_post(post.channel_id, post.message_id)  # ... not even after a restart
        await db.delete_post(post.id)
        await show(event, note, [[Button.inline("📚 My posts", "pl")]])
