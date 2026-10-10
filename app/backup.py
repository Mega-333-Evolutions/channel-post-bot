"""Putting a backup made by /export back into the bot (/import).

The backup is a JSON file: {"exported_at", "channels": [...], "posts": [...], "ignored": [...]}. Importing only ADDS:

  * a channel the bot already has is left as it is (its title, username and access come from Telegram, which is newer
    than the file); a channel it does not have is added;
  * a post at a message id the bot already has is left as it is; every other published post is added, and so is every
    draft that is not already here;
  * a post the owner has made the bot forget (/posts -> Only forget it in the bot) is not brought back;
  * nothing is ever deleted or overwritten.

The file is read as data only: every value is checked before it goes near the database.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Channel, Database, IgnoredPost, Post, as_utc, utcnow

log = logging.getLogger(__name__)

MAX_BYTES = 40 * 1024 * 1024  # a backup of tens of thousands of posts is a few megabytes
CHUNK = 500  # rows saved in one go
POST_KINDS = ("draft", "sent")
SOURCES = ("bot", "adopted")


class BackupError(ValueError):
    """The file can't be used; the message says why (and is safe to show)."""


# ------------------------------------------------------------------------------------------------ reading
def _int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} is not a whole number")
    return value


def _text(value, what: str, limit: Optional[int] = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what} is not text")
    return value[:limit] if limit else value


def _when(value, what: str, default: Optional[datetime] = None) -> Optional[datetime]:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ValueError(f"{what} is not a date")
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{what} is not a date")
    return as_utc(d)


def _rows_of(value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BackupError(f"“{what}” should be a list.")
    return value


def channel_row(row) -> dict:
    """One channel of the file as the values for a Channel row (ValueError if it is not usable)."""
    if not isinstance(row, dict):
        raise ValueError("not an object")
    cid = _int(row.get("id"), "id")
    if cid == 0:
        raise ValueError("id is 0")
    username = _text(row.get("username"), "username", 64).strip().lstrip("@") or None
    created = _when(row.get("created_at"), "created_at", utcnow())
    return dict(
        id=cid,
        access_hash=_int(row.get("access_hash", 0) or 0, "access_hash"),
        title=_text(row.get("title"), "title", 256),
        username=username,
        active=bool(row.get("active", True)),
        added_by=_int(row.get("added_by", 0) or 0, "added_by"),
        created_at=created,
    )


def _buttons(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("buttons are not a list")
    for line in value:
        if not isinstance(line, list) or not all(isinstance(b, dict) for b in line):
            raise ValueError("buttons are not rows of buttons")
    return value


def _entities(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
        raise ValueError("formatting is not a list")
    return value


def post_row(row) -> dict:
    """One post of the file as the values for a Post row (ValueError if it is not usable)."""
    if not isinstance(row, dict):
        raise ValueError("not an object")
    cid = _int(row.get("channel_id"), "channel_id")
    mid = row.get("message_id")
    mid = None if mid is None else _int(mid, "message_id")
    status = row.get("status") or ("sent" if mid else "draft")
    if status not in POST_KINDS:
        raise ValueError(f"status “{str(status)[:20]}” is unknown")
    if status == "sent" and (mid is None or mid < 1):
        raise ValueError("a published post without a message id")
    if status == "draft":
        mid = None
    source = row.get("source") if row.get("source") in SOURCES else "bot"
    kind = row.get("media_kind")
    if kind is not None and (not isinstance(kind, str) or len(kind) > 16):
        raise ValueError("media_kind is not usable")
    ref = row.get("media_file_id")
    if ref is not None and not isinstance(ref, str):
        raise ValueError("media_file_id is not text")
    created = _when(row.get("created_at"), "created_at", utcnow())
    return dict(
        channel_id=cid,
        message_id=mid,
        status=status,
        source=source,
        text=_text(row.get("text"), "text"),
        entities=_entities(row.get("entities")),
        media_kind=kind or None,
        media_file_id=ref or None,
        buttons=_buttons(row.get("buttons")),
        link_preview=bool(row.get("link_preview")),
        created_by=_int(row.get("created_by", 0) or 0, "created_by"),
        created_at=created,
        updated_at=_when(row.get("updated_at"), "updated_at", created),
        sent_at=_when(row.get("sent_at"), "sent_at"),
    )


@dataclass
class Backup:
    exported_at: Optional[str] = None
    channels: list = field(default_factory=list)  # dicts for Channel(**d)
    posts: list = field(default_factory=list)  # dicts for Post(**d)
    ignored: list = field(default_factory=list)  # (channel id, message id)
    bad_rows: int = 0  # entries of the file that were not usable and are left out


def parse_backup(raw: bytes) -> Backup:
    """The file's bytes -> a Backup. BackupError (with a message for the person) when it is not an /export file."""
    if len(raw) > MAX_BYTES:
        raise BackupError(f"it is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("it is not a JSON file. Send the file that /export made, unchanged.")
    if not isinstance(data, dict) or ("channels" not in data and "posts" not in data):
        raise BackupError("it does not look like a backup from /export (no channels and posts in it).")
    bk = Backup(exported_at=data.get("exported_at") if isinstance(data.get("exported_at"), str) else None)
    for row in _rows_of(data.get("channels"), "channels"):
        try:
            bk.channels.append(channel_row(row))
        except (ValueError, TypeError):
            bk.bad_rows += 1
    for row in _rows_of(data.get("posts"), "posts"):
        try:
            bk.posts.append(post_row(row))
        except (ValueError, TypeError):
            bk.bad_rows += 1
    for row in _rows_of(data.get("ignored"), "ignored"):
        try:
            if not isinstance(row, dict):
                raise ValueError("not an object")
            bk.ignored.append((_int(row.get("channel_id"), "channel_id"), _int(row.get("message_id"), "message_id")))
        except (ValueError, TypeError):
            bk.bad_rows += 1
    if not (bk.channels or bk.posts):
        raise BackupError("there is nothing in it that can be imported." + (" (All entries were unreadable.)" if bk.bad_rows else ""))
    return bk


# ------------------------------------------------------------------------------------------------ the plan
@dataclass
class ImportPlan:
    backup: Backup
    new_channels: list = field(default_factory=list)  # channel dicts that are not in the bot yet
    channels_present: int = 0  # channels of the file the bot has already (left as they are)
    new_posts: list = field(default_factory=list)  # published posts and drafts to add
    posts_present: int = 0  # published posts at a message id the bot already has
    drafts_present: int = 0  # drafts that are here already
    posts_forgotten: int = 0  # posts the owner made the bot forget: not brought back
    posts_orphaned: int = 0  # posts of a channel that is neither in the bot nor in the file
    new_ignored: list = field(default_factory=list)  # "forgotten" marks to add

    @property
    def sent_to_add(self) -> int:
        return sum(1 for p in self.new_posts if p["status"] == "sent")

    @property
    def drafts_to_add(self) -> int:
        return sum(1 for p in self.new_posts if p["status"] == "draft")

    @property
    def nothing_new(self) -> bool:
        return not (self.new_channels or self.new_posts or self.new_ignored)


async def plan_import(db: Database, backup: Backup) -> ImportPlan:
    """What importing would do - nothing is written."""
    plan = ImportPlan(backup=backup)
    async with db.Session() as s:
        have_channels = {int(c) for c in (await s.execute(select(Channel.id))).scalars()}
        known_marks = {(int(c), int(m)) for c, m in (await s.execute(select(IgnoredPost.channel_id, IgnoredPost.message_id))).all()}
        wanted = {p["channel_id"] for p in backup.posts}
        have_posts: set = set()
        have_drafts: set = set()
        if wanted:
            q = select(Post.channel_id, Post.message_id).where(Post.message_id.is_not(None))
            have_posts = {(int(c), int(m)) for c, m in (await s.execute(q)).all() if int(c) in wanted}
            q = select(Post.channel_id, Post.text, Post.created_at).where(Post.status == "draft")
            have_drafts = {(int(c), t, as_utc(at)) for c, t, at in (await s.execute(q)).all() if int(c) in wanted}
    in_file: dict = {}
    for ch in backup.channels:
        if ch["id"] in have_channels:
            plan.channels_present += 1
        elif ch["id"] not in in_file:
            in_file[ch["id"]] = ch
            plan.new_channels.append(ch)
    marks = set(known_marks)
    for mark in backup.ignored:
        if mark[0] in have_channels or mark[0] in in_file:
            if mark not in marks:
                marks.add(mark)
                plan.new_ignored.append(mark)
    seen_posts: set = set()
    seen_drafts: set = set()
    for p in backup.posts:
        cid = p["channel_id"]
        if cid not in have_channels and cid not in in_file:
            plan.posts_orphaned += 1
        elif p["status"] == "sent":
            key = (cid, p["message_id"])
            if key in marks:
                plan.posts_forgotten += 1
            elif key in have_posts or key in seen_posts:
                plan.posts_present += 1
            else:
                seen_posts.add(key)
                plan.new_posts.append(p)
        else:
            key = (cid, p["text"], p["created_at"])
            if key in have_drafts or key in seen_drafts:
                plan.drafts_present += 1
            else:
                seen_drafts.add(key)
                plan.new_posts.append(p)
    return plan


# ----------------------------------------------------------------------------------------------- doing it
@dataclass
class ImportResult:
    channels_added: int = 0
    posts_added: int = 0
    drafts_added: int = 0
    marks_added: int = 0
    skipped_meanwhile: int = 0  # posts that turned up in the bot while the import was running


async def _save_posts(db: Database, rows: list) -> list:
    """Save these posts; returns the ones that were saved (one that is there already is skipped, never an error)."""
    async with db.Session() as s:
        s.add_all([Post(**r) for r in rows])
        try:
            await s.commit()
            return list(rows)
        except IntegrityError:
            await s.rollback()
    saved = []
    for r in rows:  # one is there already: one by one
        async with db.Session() as s:
            s.add(Post(**r))
            try:
                await s.commit()
                saved.append(r)
            except IntegrityError:
                await s.rollback()
    return saved


async def apply_import(db: Database, plan: ImportPlan, progress=None) -> ImportResult:
    """Write the plan: channels first (posts point at them), then the posts in pieces, then the "forgotten" marks."""
    res = ImportResult()
    if plan.new_channels:
        async with db.Session() as s:
            for d in plan.new_channels:
                if await s.get(Channel, d["id"]) is None:
                    s.add(Channel(**d))
                    res.channels_added += 1
            await s.commit()
    rows = plan.new_posts
    for i in range(0, len(rows), CHUNK):
        part = rows[i : i + CHUNK]
        saved = await _save_posts(db, part)
        res.posts_added += sum(1 for r in saved if r["status"] == "sent")
        res.drafts_added += sum(1 for r in saved if r["status"] == "draft")
        res.skipped_meanwhile += len(part) - len(saved)
        if progress is not None:
            await progress(min(i + CHUNK, len(rows)), len(rows))
    for cid, mid in plan.new_ignored:
        await db.ignore_post(cid, mid)
        res.marks_added += 1
    return res


def stamp(iso: Optional[str]) -> str:
    """'2026-10-07T10:00:00+00:00' -> '07 Oct 2026, 10:00 UTC' (the text as it is when it can't be read)."""
    if not iso:
        return ""
    try:
        d = as_utc(datetime.fromisoformat(iso.replace("Z", "+00:00")))
    except ValueError:
        return iso[:40]
    return d.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
