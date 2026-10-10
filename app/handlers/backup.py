"""/import (owner only): put a backup made by /export back.

The steps: /import → send the file → a preview of what would be added → ✅ Import. Importing only adds, it never
changes or deletes anything that is here (see app/backup.py for the rules).
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field

from telethon import Button

from ..backup import MAX_BYTES, BackupError, ImportPlan, ImportResult, apply_import, parse_backup, plan_import, stamp
from ..common import (
    Ctx,
    UserError,
    clear_state,
    cmd,
    guard,
    on_cb,
    on_text,
    purge_pending,
    require_owner,
    say,
    set_state,
    show,
)

log = logging.getLogger(__name__)

ASK = (
    "📥 <b>Import a backup</b>\n"
    "Send me the file that /export made (<code>channel-posts-export.json</code>) as your next message - the file "
    "itself, unchanged.\n\n"
    "Importing only <b>adds</b>: channels and posts that are here already are left exactly as they are, and nothing is "
    "deleted. You see what would be added before anything happens. /cancel stops."
)


@dataclass
class PendingImport:
    """A backup that was read and is waiting for the owner's OK (or is being imported)."""

    user: int
    plan: ImportPlan
    running: bool = False
    created: float = field(default_factory=time.monotonic)


def preview_text(plan: ImportPlan) -> str:
    bk = plan.backup
    sent_total = sum(1 for p in bk.posts if p["status"] == "sent")
    drafts_total = len(bk.posts) - sent_total
    made = f" (made {stamp(bk.exported_at)})" if bk.exported_at else ""
    lines = [f"📥 <b>Backup file read</b>{made}", ""]
    lines.append(f"📢 Channels: {len(bk.channels)} in the file - {len(plan.new_channels)} new, {plan.channels_present} here already")
    lines.append(f"📝 Published posts: {sent_total} in the file - {plan.sent_to_add} to add, {plan.posts_present} here already")
    if drafts_total:
        lines.append(f"🗒 Drafts: {drafts_total} in the file - {plan.drafts_to_add} to add, {plan.drafts_present} here already")
    if plan.new_ignored:
        lines.append(f"🙈 {len(plan.new_ignored)} post(s) that were made to be forgotten stay forgotten here too")
    if plan.posts_forgotten:
        lines.append(f"🙈 {plan.posts_forgotten} post(s) you made this bot forget are not brought back")
    if plan.posts_orphaned:
        lines.append(f"• {plan.posts_orphaned} post(s) belong to a channel that is neither here nor in the file - left out")
    if bk.bad_rows:
        lines.append(f"• {bk.bad_rows} entr{'y' if bk.bad_rows == 1 else 'ies'} of the file could not be read - left out")
    lines.append("")
    if plan.nothing_new:
        lines.append("There is nothing new in this file - everything in it is here already.")
    else:
        lines.append("Nothing that is here now is changed or deleted.")
        if plan.new_channels:
            lines.append(
                "A channel that is new here works only if this bot is an admin there. On a different bot, send "
                "/addchannel for it once afterwards - its posts stay."
            )
    return "\n".join(lines)


def result_text(res: ImportResult) -> str:
    lines = ["✅ <b>Imported</b>", ""]
    if res.channels_added:
        lines.append(
            f"📢 {res.channels_added} channel(s) added - check them in /channels "
            "(on a different bot, send /addchannel for each one once so the bot gets its own access)"
        )
    lines.append(f"📝 {res.posts_added} published post(s) added to My posts")
    if res.drafts_added:
        lines.append(f"🗒 {res.drafts_added} draft(s) added")
    if res.marks_added:
        lines.append(f"🙈 {res.marks_added} “forgotten” mark(s) added")
    if res.skipped_meanwhile:
        lines.append(f"• {res.skipped_meanwhile} post(s) turned up here meanwhile and were left as they are")
    return "\n".join(lines)


def register(ctx: Ctx) -> None:
    client, db = ctx.client, ctx.db

    @client.on(cmd("import"))
    @guard(ctx, owner=True)
    async def h_import(event):
        set_state(ctx, event.sender_id, "import")
        await say(event, ASK, [[Button.inline("✖️ Cancel", "cx")]])

    @on_text(ctx, "import")
    async def got_file(event, st):
        require_owner(ctx, event)
        msg = event.message
        f = getattr(msg, "file", None)
        if f is None:
            raise UserError("Please send the backup FILE (the one /export made), not text. /cancel stops.")
        if (getattr(f, "size", None) or 0) > MAX_BYTES:
            raise UserError(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB - it is not a backup from /export. Send the right file, or /cancel.")
        note = await say(event, "📥 Reading the file…")
        data = await msg.download_media(file=bytes)
        if not isinstance(data, (bytes, bytearray)):
            raise UserError("I could not download that file. Send it again, or /cancel.")
        try:
            plan = await plan_import(db, parse_backup(bytes(data)))
        except BackupError as e:
            raise UserError(f"That file can't be imported: {e}\nSend the right file, or /cancel.")
        clear_state(ctx, event.sender_id)
        purge_pending(ctx)
        if plan.nothing_new:
            await note.edit(preview_text(plan), buttons=None)
            return
        token = secrets.token_hex(4)
        ctx.pending[token] = PendingImport(user=event.sender_id, plan=plan)
        kb = [[Button.inline("✅ Import", f"imy:{token}"), Button.inline("✖️ Cancel", f"imx:{token}")]]
        await note.edit(preview_text(plan), buttons=kb)

    @on_cb(ctx, "imx")
    async def cb_cancel(event, parts):
        require_owner(ctx, event)
        pend = ctx.pending.get(parts[0])
        if isinstance(pend, PendingImport) and not pend.running:
            ctx.pending.pop(parts[0], None)
        await show(event, "Cancelled - nothing was imported.")

    @on_cb(ctx, "imy")
    async def cb_import(event, parts):
        require_owner(ctx, event)
        pend = ctx.pending.get(parts[0])
        if not isinstance(pend, PendingImport):
            raise UserError("This import has expired (or was done already). Send /import again.")
        if pend.running:
            raise UserError("This import is already running.")
        if ctx.lock.locked():
            raise UserError("Another long job (replace, repost, shift, sync ...) is still running - try again when it has finished.")
        last = [0.0]

        async def prog(done: int, total: int) -> None:
            if time.monotonic() - last[0] < 3:
                return
            last[0] = time.monotonic()
            try:
                await show(event, f"📥 Importing… {done} of {total} post(s)")
            except Exception:
                log.debug("progress update failed", exc_info=True)

        async with ctx.lock:
            pend.running = True
            try:
                res = await apply_import(db, pend.plan, progress=prog)
            finally:
                ctx.pending.pop(parts[0], None)
        if ctx.syncer is not None:
            ctx.syncer.forget_cache()  # the channels that came in are known to the live watch from now on
        await show(event, result_text(res), [[Button.inline("📚 My posts", "pl")]])
