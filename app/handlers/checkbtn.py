"""Check buttons (owner only): compare the buttons saved in the bot with what a channel really shows, and repair."""
from __future__ import annotations

import logging
import time

from telethon import Button, errors

from ..buttons_sync import fix_buttons, scan_buttons
from ..common import Ctx, UserError, on_cb, require_owner, show
from ..tgutil import esc

log = logging.getLogger(__name__)
SAMPLE = 12


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg

    async def load(parts):
        ch = await db.get_channel(int(parts[0]))
        if ch is None:
            raise UserError("That channel is not registered.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace / repost) is still running.")
        return ch

    def throttled(event, make_text, every: float = 3.0):
        last = [0.0]

        async def upd(obj, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last[0] < every:
                return
            last[0] = now
            try:
                await show(event, make_text(obj))
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                log.debug("progress update failed", exc_info=True)

        return upd

    @on_cb(ctx, "pk")
    async def cb_check(event, parts):
        require_owner(ctx, event)
        ch = await load(parts)
        title = esc(ch.title)
        async with ctx.lock:
            await show(event, f"🔧 Checking the buttons of <b>{title}</b>…")
            posts = await db.sent_posts(ch.id)
            scan = await scan_buttons(
                client, ch, posts, progress=throttled(event, lambda s: f"🔧 Checking <b>{title}</b>: {s.fine + len(s.bad) + len(s.gone)}/{s.total}…")
            )
        lines = [f"🔧 <b>{title}</b> - checked {scan.total} post(s) that have link buttons."]
        kb = []
        if not scan.total:
            lines.append("None of the saved posts has link buttons, so there is nothing to check.")
        else:
            lines.append(f"✅ {scan.fine} show their buttons correctly.")
        if scan.bad:
            ids = ", ".join(f"#{p.message_id}" for p, _, _ in scan.bad[:SAMPLE])
            more = "…" if len(scan.bad) > SAMPLE else ""
            lines.append(
                f"⚠️ <b>{len(scan.bad)}</b> are saved with buttons here, but the channel shows them without those buttons "
                f"(or with different links): {ids}{more}"
            )
            kb.append([Button.inline(f"🔧 Fix {len(scan.bad)} post(s)", f"pkf:{ch.id}")])
        elif scan.total:
            lines.append("Everything matches.")
        if scan.gone:
            ids = ", ".join(f"#{p.message_id}" for p in scan.gone[:SAMPLE])
            lines.append(
                f"ℹ️ {len(scan.gone)} saved post(s) no longer exist in the channel ({ids}{'…' if len(scan.gone) > SAMPLE else ''})."
            )
        kb.append([Button.inline("🔙 Back", f"pc:{ch.id}:0")])
        await show(event, "\n".join(lines), kb)

    @on_cb(ctx, "pkf")
    async def cb_fix(event, parts):
        require_owner(ctx, event)
        ch = await load(parts)
        title = esc(ch.title)
        async with ctx.lock:
            await show(event, f"🔧 Looking again at <b>{title}</b>…")
            scan = await scan_buttons(client, ch, await db.sent_posts(ch.id))
            if not scan.bad:
                res = None
            else:
                total = len(scan.bad)
                res = await fix_buttons(
                    client,
                    ch,
                    scan.bad,
                    delay=cfg.edit_delay,
                    progress=throttled(event, lambda r: f"🔧 Fixing <b>{title}</b>: {r.fixed + r.already}/{total}…"),
                )
        if res is None:
            text = f"✅ <b>{title}</b>: all buttons match now - nothing to fix."
        else:
            lines = [f"🔧 <b>{title}</b> - put the saved buttons back on <b>{res.fixed}</b> post(s)."]
            if res.nudged:
                lines.append(f"{res.nudged} of them needed a second edit before Telegram showed the buttons.")
            if res.already:
                lines.append(f"{res.already} were already fine when I got to them.")
            if res.gone:
                lines.append(f"{res.gone} no longer exist in the channel.")
            if res.failed:
                sample = ", ".join(f"#{m} {esc(n)}" for m, n in res.failed[:8])
                lines.append(f"⚠️ <b>{len(res.failed)} failed</b> ({sample}{'…' if len(res.failed) > 8 else ''}).")
            if res.aborted:
                lines.append(f"Stopped early: Telegram refused {esc(res.aborted)} several times in a row.")
            text = "\n".join(lines)
        await show(event, text, [[Button.inline("🔧 Check again", f"pk:{ch.id}"), Button.inline("🔙 Back", f"pc:{ch.id}:0")]])
