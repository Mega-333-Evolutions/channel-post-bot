"""New links, one button after the other.

* "Mass replace links" in the panel of a published button post: the bot asks for a new link for that post's button and,
  after each answer, for the next button post of the channel (posts without a link button are skipped).
* /autopost uses the same steps to ask for the real link of every button it posted.

The position in the list lives in the user's state; each answer changes the button in the channel at once.
"""
from __future__ import annotations

import logging

from telethon import Button, errors

from ..buttons import link_positions, norm_url, set_field
from ..common import Ctx, UserError, clear_state, is_cb, on_cb, on_text, say, set_state, show
from ..tgutil import build_markup, edit_raw, esc, explain_rpc, flood_retry, peer_of, post_link, short
from .panel import load_post

log = logging.getLogger(__name__)

MODE = "link_queue"
HEADINGS = {"mass": "🔗 <b>Mass replace links</b>", "auto": "🔗 <b>Links for the new posts</b>"}


async def set_link(ctx: Ctx, post, ch, r: int, c: int, url: str):
    """Point button (r, c) of a published post at `url`: the buttons of the channel message change (only the buttons,
    the text stays as it is), then the saved post follows."""
    rows = set_field(post.buttons, r, c, u=url)
    if post.status == "sent" and post.message_id:
        try:
            await flood_retry(lambda: edit_raw(ctx.client, peer_of(ch), post.message_id, markup=build_markup(rows)))
        except errors.MessageNotModifiedError:
            pass  # it already had that link
        except errors.RPCError as e:
            raise UserError("❌ " + explain_rpc(e) + "\nThe link of this button was NOT changed.")
    return await ctx.db.update_post(post.id, buttons=rows)


def mass_items(posts) -> list:
    """[(post id, row, column)] of every link button in `posts`, in the order given."""
    return [[p.id, r, c] for p in posts for r, c in link_positions(p.buttons)]


async def current_target(ctx: Ctx, st: dict):
    """(post, channel, row, column) the list is at, moving past buttons that are gone; None when the list is finished."""
    while st["i"] < len(st["items"]):
        pid, r, c = st["items"][st["i"]]
        post = await ctx.db.get_post(pid)
        if post is not None and post.status == "sent" and post.message_id and (r, c) in link_positions(post.buttons):
            ch = await ctx.db.get_channel(post.channel_id)
            if ch is not None:
                return post, ch, r, c
        st["i"] += 1
        st["gone"] += 1
    return None


def summary(st: dict, stopped: bool) -> str:
    left = max(0, len(st["items"]) - st["i"]) if stopped else 0
    bits = [f"{st['done']} link(s) replaced"]
    if st["skipped"]:
        bits.append(f"{st['skipped']} skipped")
    if st["gone"]:
        bits.append(f"{st['gone']} button(s) no longer there")
    if left:
        bits.append(f"{left} not asked")
    return ("Stopped. " if stopped else "✅ <b>All done.</b> ") + ", ".join(bits) + "."


def prompt_for(st: dict, post, ch, r: int, c: int) -> str:
    total = len(st["items"])
    b = post.buttons[r][c]
    lines = [f"{HEADINGS[st['kind']]} · <b>{esc(ch.title)}</b> ({st['i'] + 1} of {total})", ""]
    where = f"Post #{post.id}"
    if post.message_id:
        where = f'<a href="{post_link(ch, post.message_id)}">Post #{post.id}</a>'
    several = len(link_positions(post.buttons))
    label = f"“{esc(short(b.get('t') or '', 60))}”"
    lines.append(f"{where} · button {label}" + (f" (button {r + 1}.{c + 1} of {several} links)" if several > 1 else ""))
    lines.append(f"Now: <code>{esc(b.get('u') or '')}</code>")
    lines += ["", "🔗 Send the new link for this button."]
    return "\n".join(lines)


async def ask(ctx: Ctx, event, st: dict, lead: str = "", *, new: bool = True) -> None:
    """Ask for the next link, or finish. `lead` (what just happened) goes in front, in the same message."""
    tgt = await current_target(ctx, st)
    kb_done = [[Button.inline("📚 My posts", "pl")]]
    send = say if (new or not is_cb(event)) else show
    if tgt is None:
        clear_state(ctx, event.sender_id)
        await send(event, (lead + "\n\n" if lead else "") + summary(st, False), kb_done)
        return
    post, ch, r, c = tgt
    kb = [[Button.inline("⏭ Skip this one", f"lqs:{st['i']}"), Button.inline("✖️ Cancel", "lqx")]]
    await send(event, (lead + "\n\n" if lead else "") + prompt_for(st, post, ch, r, c), kb)


async def begin_queue(ctx: Ctx, event, *, kind: str, cid: int, items: list, lead: str = "", new: bool = True) -> None:
    """Start asking: `items` is [(post id, row, column)] in the order the links are wanted."""
    st = set_state(ctx, event.sender_id, MODE, kind=kind, cid=cid, items=[list(i) for i in items], i=0, done=0, skipped=0, gone=0)
    await ask(ctx, event, st, lead, new=new)


def register(ctx: Ctx) -> None:
    db = ctx.db

    # ------------------------------------------------------------- the panel option
    @on_cb(ctx, "mlp")
    async def cb_mass_start(event, parts):
        post, ch = await load_post(ctx, parts[0])
        if post.status != "sent" or not post.message_id:
            raise UserError("Mass replace works on published posts only.")
        if not link_positions(post.buttons):
            raise UserError("This post has no button with a link.")
        posts = await db.sent_posts_from(ch.id, post.message_id)
        await begin_queue(ctx, event, kind="mass", cid=ch.id, items=mass_items(posts), new=False)

    # ---------------------------------------------------------------- the answers
    @on_text(ctx, MODE)
    async def h_link(event, st):
        url = norm_url(event.raw_text or "")
        if not url:
            raise UserError("That doesn't look like a link. Send something like https://t.me/yourbot?start=abc")
        tgt = await current_target(ctx, st)
        if tgt is None:  # everything this list was about has vanished meanwhile
            await ask(ctx, event, st)
            return
        post, ch, r, c = tgt
        label = post.buttons[r][c].get("t") or ""
        await set_link(ctx, post, ch, r, c, url)
        st["i"] += 1
        st["done"] += 1
        await ask(ctx, event, st, f"✅ <b>{esc(short(label, 40))}</b> now opens <code>{esc(url)}</code>")

    def live(event, parts):
        st = ctx.state.get(event.sender_id)
        if not st or st.get("mode") != MODE:
            raise UserError("That list is no longer active.")
        if parts and parts[0].isdigit() and int(parts[0]) != st["i"]:
            raise UserError("That button belongs to an earlier step - use the newest message.")
        return st

    @on_cb(ctx, "lqs")
    async def cb_skip(event, parts):
        st = live(event, parts)
        st["i"] += 1
        st["skipped"] += 1
        await ask(ctx, event, st, "⏭ Skipped.", new=False)

    @on_cb(ctx, "lqx")
    async def cb_stop(event, parts):
        st = ctx.state.get(event.sender_id)
        if not st or st.get("mode") != MODE:
            await show(event, "Cancelled.")
            return
        clear_state(ctx, event.sender_id)
        await show(event, summary(st, True), [[Button.inline("📚 My posts", "pl")]])
