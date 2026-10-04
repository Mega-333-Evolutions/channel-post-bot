"""/replace and /undo (owner only)."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from telethon import Button, errors

from ..common import Ctx, UserError, cmd, edit_callback_message, guard, on_cb, purge_pending, require_owner, say, show
from ..linkswap import normalize_username
from ..replace_engine import ApplyResult, ChannelScan, ScanOptions, apply_channel, scan_channel, undo_batch
from ..tgutil import esc, explain_rpc, get_rights
from .channels import parse_channel_ref

log = logging.getLogger(__name__)

USAGE = (
    "Usage: <code>/replace @old @new</code>\n"
    "Options: <code>--channel @name</code> (only that channel), <code>--last 200</code> (only the newest 200 "
    "post ids), <code>--posts</code> (also change t.me/old/123 post links), <code>--no-typed</code> (don't touch "
    "links typed in the text)."
)


@dataclass
class Job:
    user: int
    opts: ScanOptions
    scans: list
    running: bool = False
    created: float = field(default_factory=time.monotonic)


def parse_replace_args(raw: str) -> tuple:
    toks = (raw or "").split()
    flags = {"channel": None, "last": None, "posts": False, "typed": None}
    pos: list = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--channel" and i + 1 < len(toks):
            flags["channel"] = toks[i + 1]
            i += 2
        elif t == "--last" and i + 1 < len(toks):
            if not toks[i + 1].isdigit() or int(toks[i + 1]) < 1:
                raise ValueError("--last needs a number, for example --last 200")
            flags["last"] = int(toks[i + 1])
            i += 2
        elif t == "--posts":
            flags["posts"] = True
            i += 1
        elif t == "--no-typed":
            flags["typed"] = False
            i += 1
        elif t.startswith("--"):
            raise ValueError(f"Unknown option {t}")
        else:
            pos.append(t)
            i += 1
    if len(pos) != 2:
        raise ValueError("Give the old and the new username.")
    old, new = normalize_username(pos[0]), normalize_username(pos[1])
    if not old or not new:
        raise ValueError("Usernames look like @name (letters, digits and underscores).")
    if old.lower() == new.lower():
        raise ValueError("The old and the new username are the same.")
    return old, new, flags


def pick_channels(chans: list, ref) -> list:
    if not ref:
        return chans
    r = parse_channel_ref(ref)
    out = [c for c in chans if (isinstance(r, int) and c.id == r) or (isinstance(r, str) and (c.username or "").lower() == r.lower())]
    if not out:
        raise ValueError("That channel isn't registered (see /channels).")
    return out


def summary_text(job: Job) -> str:
    o = job.opts
    lines = [f"🔎 <b>Scan finished</b> - replace <code>@{esc(o.old)}</code> → <code>@{esc(o.new)}</code>"]
    total = 0
    for s in job.scans:
        head = f"\n<b>{esc(s.channel.title)}</b>"
        if s.error:
            lines.append(f"{head}\n⚠️ {esc(s.error)}")
            continue
        block = f"{head} - looked at post ids {s.first}-{s.scanned}"
        if s.total == 0:
            block += "\nNothing to change."
        else:
            b = sum(i.buttons for i in s.infos.values())
            h = sum(i.links for i in s.infos.values())
            t = sum(i.typed for i in s.infos.values())
            block += f"\n{s.total} post(s) to change: {b} button link(s), {h} hyperlink(s), {t} typed link(s)"
            if s.foreign:
                block += f"\n• {s.foreign} of them were not posted by this bot"
            fb = sum(1 for i in s.infos.values() if not i.mine and i.buttons)
            if fb:
                block += f"\n• {fb} are button posts made by someone else - Telegram may refuse to edit those; /repost can re-post them with the new links instead"
            cb = sum(1 for i in s.infos.values() if i.callbacks)
            if cb:
                block += f"\n• {cb} carry other bots' buttons (e.g. reactions) - those stay as they are"
        if s.skipped_post_links:
            block += f"\n• skipped {s.skipped_post_links} link(s) to channel posts (t.me/{o.old}/123) - use --posts to change them too"
        if s.not_editable:
            block += f"\n⚠️ {s.not_editable} post(s) by others can't be edited: the bot lacks “Edit messages of others” here"
        total += s.total
        lines.append(block)
    lines.append(f"\n<b>Total: {total} post(s).</b>")
    if total:
        lines.append("Nothing has been changed yet. Every edit is logged, so /undo can restore the old versions.")
    return "\n".join(lines)


def register(ctx: Ctx) -> None:
    client, db, cfg = ctx.client, ctx.db, ctx.cfg
    lock = ctx.lock

    def make_updater(event):
        last = [0.0]

        async def upd(text: str, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last[0] < 3:
                return
            last[0] = now
            try:
                await edit_callback_message(event, text)
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                log.debug("progress update failed", exc_info=True)

        return upd

    # ------------------------------------------------------------------ /replace
    @client.on(cmd("replace", args=True))
    @guard(ctx, owner=True)
    async def h_replace(event):
        purge_pending(ctx)
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            await say(event, USAGE)
            return
        try:
            old, new, flags = parse_replace_args(raw)
            chans = pick_channels(await db.list_channels(), flags["channel"])
        except ValueError as e:
            raise UserError(f"{e}\n\n" + re.sub(r"<[^>]+>", "", USAGE))
        if not chans:
            raise UserError("No channels registered yet. Use /addchannel.")
        if lock.locked():
            raise UserError("Another /replace or /undo is still running - wait for it to finish.")
        opts = ScanOptions(
            old=old,
            new=new,
            include_typed=cfg.replace_typed_links if flags["typed"] is None else flags["typed"],
            include_posts=flags["posts"],
            last=flags["last"],
        )
        async with lock:
            status = await say(event, f"🔍 Scanning {len(chans)} channel(s)…")
            last_edit = [0.0]
            scans = []
            for ch in chans:
                rights = await get_rights(client, ch)
                if rights is not None and not rights.admin:
                    s = await _empty_scan(ch, "The bot is not an admin here any more.")
                    scans.append(s)
                    continue
                can_edit = True if rights is None else rights.edit

                async def prog(scan, title=ch.title):
                    now = time.monotonic()
                    if now - last_edit[0] < 3:
                        return
                    last_edit[0] = now
                    try:
                        await status.edit(f"🔍 Scanning <b>{esc(title)}</b>: post id {scan.scanned} of {scan.top or '?'}…")
                    except Exception:
                        pass

                try:
                    scan = await scan_channel(
                        client, ch, opts, can_edit_others=can_edit, mine_ids=await db.mine_message_ids(ch.id), progress=prog
                    )
                except errors.RPCError as e:
                    scan = await _empty_scan(ch, explain_rpc(e))
                scan.rights = rights
                scans.append(scan)
        job = Job(user=event.sender_id, opts=opts, scans=scans)
        token = secrets.token_hex(4)
        ctx.pending[token] = job
        total = sum(s.total for s in scans)
        kb = None
        if total:
            kb = [[Button.inline(f"✅ Apply to {total} post(s)", f"rp:{token}:y"), Button.inline("✖️ Cancel", f"rp:{token}:n")]]
        try:
            await status.edit(summary_text(job), buttons=kb, link_preview=False)
        except Exception:
            await say(event, summary_text(job), kb)

    async def _empty_scan(ch, error: str):
        return ChannelScan(channel=ch, error=error)

    @on_cb(ctx, "rp")
    async def cb_replace(event, parts):
        require_owner(ctx, event)
        token, action = parts[0], parts[1]
        job = ctx.pending.get(token)
        if not isinstance(job, Job) or job.user != event.sender_id:
            raise UserError("That scan has expired. Run /replace again.")
        if action == "n":
            ctx.pending.pop(token, None)
            await show(event, "Cancelled - nothing was changed.")
            return
        if job.running or lock.locked():
            raise UserError("It is already running.")
        job.running = True
        upd = make_updater(event)
        batch_id = secrets.token_hex(6)
        o = job.opts
        totals = ApplyResult()
        stopped: list = []
        async with lock:
            await db.create_batch(batch_id, o.old, o.new, event.sender_id)
            todo = sum(s.total for s in job.scans)
            done_before = 0
            for s in job.scans:
                if not s.infos:
                    continue

                async def prog(res, s=s, base=done_before):
                    await upd(f"⏳ <b>{esc(s.channel.title)}</b> - {base + res.edited}/{todo} edited…")

                try:
                    res = await apply_channel(
                        client, db, s, o, batch_id, event.sender_id, edit_delay=cfg.edit_delay, progress=prog
                    )
                except Exception as e:
                    log.exception("apply failed in %s", s.channel.id)
                    totals.failed.append((0, f"{s.channel.title}: {type(e).__name__}"))
                    continue
                done_before += res.edited
                totals.edited += res.edited
                totals.unchanged += res.unchanged
                totals.missing += res.missing
                totals.no_perm += res.no_perm
                totals.failed += res.failed
                if res.aborted:
                    stopped.append((s.channel.title, res.aborted))
        ctx.pending.pop(token, None)
        lines = [f"✅ <b>Done</b> - <code>@{esc(o.old)}</code> → <code>@{esc(o.new)}</code>", f"• edited: <b>{totals.edited}</b>"]
        if totals.unchanged:
            lines.append(f"• already fine / changed meanwhile: {totals.unchanged}")
        if totals.missing:
            lines.append(f"• deleted meanwhile: {totals.missing}")
        if totals.no_perm:
            lines.append(f"• skipped (no permission): {totals.no_perm}")
        if totals.failed:
            sample = ", ".join(f"#{m} {esc(n)}" for m, n in totals.failed[:8])
            lines.append(f"• <b>failed: {len(totals.failed)}</b> ({sample}{'…' if len(totals.failed) > 8 else ''})")
            names = {n for _, n in totals.failed}
            if names & {"MessageAuthorRequiredError", "InlineBotRequiredError", "ChatAdminRequiredError", "MessageIdInvalidError"}:
                lines.append("  Telegram doesn't let this bot edit those posts. Use /repost to post them again with the new links.")
        for title, name in stopped:
            lines.append(f"⚠️ Stopped early in <b>{esc(title)}</b>: Telegram refused 5 edits in a row ({esc(name)}).")
        kb = [[Button.inline("↩️ Undo this replace", f"ruc:{batch_id}")]] if totals.edited else None
        if kb:
            try:
                await edit_callback_message(event, "\n".join(lines), kb)
            except errors.MessageNotModifiedError:
                pass
        else:
            await upd("\n".join(lines), force=True)

    # -------------------------------------------------------------------- /undo
    async def ask_undo(event, batch) -> None:
        n = await db.count_batch_changes(batch.id)
        await say(
            event,
            f"↩️ Undo the replace <code>@{esc(batch.old_username)}</code> → <code>@{esc(batch.new_username)}</code>?\n"
            f"{n} post(s) will be put back exactly as they were before.",
            [[Button.inline("✅ Yes, undo", f"ru:{batch.id}"), Button.inline("✖️ No", "cx")]],
        )

    @client.on(cmd("undo"))
    @guard(ctx, owner=True)
    async def h_undo(event):
        batch = await db.last_batch()
        if batch is None:
            raise UserError("There is no /replace run to undo.")
        await ask_undo(event, batch)

    @on_cb(ctx, "ruc")
    async def cb_undo_ask(event, parts):
        require_owner(ctx, event)
        batch = await db.get_batch(parts[0])
        if batch is None or batch.undone:
            raise UserError("That replace was already undone.")
        await ask_undo(event, batch)

    @on_cb(ctx, "ru")
    async def cb_undo(event, parts):
        require_owner(ctx, event)
        if lock.locked():
            raise UserError("Another /replace or /undo is still running.")
        upd = make_updater(event)
        async with lock:
            await upd("⏳ Restoring…", force=True)

            async def prog(res):
                await upd(f"⏳ Restoring… {res.restored} done")

            res = await undo_batch(client, db, parts[0], edit_delay=cfg.edit_delay, progress=prog)
        text = f"↩️ Restored <b>{res.restored}</b> post(s)."
        if res.failed:
            text += f"\n⚠️ {len(res.failed)} could not be restored (" + ", ".join(f"#{m} {esc(n)}" for m, n in res.failed[:6]) + ")."
        await upd(text, force=True)
