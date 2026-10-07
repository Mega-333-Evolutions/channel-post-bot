"""Database layer (SQLAlchemy 2.0 async). PostgreSQL in production, SQLite for local tests."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

log = logging.getLogger(__name__)

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    access_hash: Mapped[int] = mapped_column(BigInteger, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("channel_id", "message_id", name="uq_post_channel_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("channels.id"), index=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft | sent
    source: Mapped[str] = mapped_column(String(16), default="bot")  # bot | adopted
    text: Mapped[str] = mapped_column(Text, default="")
    entities: Mapped[list] = mapped_column(JSONType, default=list)
    media_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    media_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    buttons: Mapped[list] = mapped_column(JSONType, default=list)
    link_preview: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def as_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Batch(Base):
    """One /replace run (so it can be undone)."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    old_username: Mapped[str] = mapped_column(String(64))
    new_username: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)


class Change(Base):
    __tablename__ = "changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(32), ForeignKey("batches.id"), index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    old_state: Mapped[dict] = mapped_column(JSONType, default=dict)
    new_state: Mapped[dict] = mapped_column(JSONType, default=dict)
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Migration(Base):
    """One /repost run: a channel copied post by post, so it can be continued, undone or finished."""

    __tablename__ = "migrations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    old_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    new_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    include_typed: Mapped[bool] = mapped_column(Boolean, default=True)
    include_posts: Mapped[bool] = mapped_column(Boolean, default=False)
    first_id: Mapped[int] = mapped_column(BigInteger)
    last_id: Mapped[int] = mapped_column(BigInteger)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    # copying | stopped | incomplete | copied | old_deleted | copies_deleted
    status: Mapped[str] = mapped_column(String(16), default="copying")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MigrationItem(Base):
    __tablename__ = "migration_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(String(32), ForeignKey("migrations.id"), index=True)
    old_id: Mapped[int] = mapped_column(BigInteger)
    new_id: Mapped[int] = mapped_column(BigInteger)
    old_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    new_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Shift(Base):
    """One /shift run: posts of a source channel copied into another channel (so it can be continued or undone)."""

    __tablename__ = "shifts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    src_channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    src_access_hash: Mapped[int] = mapped_column(BigInteger, default=0)
    src_title: Mapped[str] = mapped_column(String(256), default="")
    src_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dst_channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    first_id: Mapped[int] = mapped_column(BigInteger)
    last_id: Mapped[int] = mapped_column(BigInteger)
    # copying | stopped | incomplete | copied | copies_deleted
    status: Mapped[str] = mapped_column(String(16), default="copying")
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShiftItem(Base):
    __tablename__ = "shift_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[str] = mapped_column(String(32), ForeignKey("shifts.id"), index=True)
    old_id: Mapped[int] = mapped_column(BigInteger)  # message id in the source channel
    new_id: Mapped[int] = mapped_column(BigInteger)  # message id of the copy in the destination channel
    new_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class ScheduledDelete(Base):
    """A channel message the bot has to delete at a given time (a broadcast with an expiry)."""

    __tablename__ = "scheduled_deletes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    delete_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    group_id: Mapped[str] = mapped_column(String(32), default="")  # the broadcast it belongs to
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelSync(Base):
    """Where the last look at a channel stopped, so the next one only has to read what is new."""

    __tablename__ = "channel_sync"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_top: Mapped[int] = mapped_column(BigInteger, default=0)  # the newest message id that existed then
    last_pts: Mapped[int] = mapped_column(BigInteger, default=0)  # the channel's event counter then
    deferred: Mapped[int] = mapped_column(Integer, default=0)  # new messages left for the next look (too fresh then)
    last_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_deep: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # last look at every id


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """A datetime from the database as an aware UTC one (SQLite hands back naive values)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def normalize_db_url(url: str) -> tuple:
    """Turn a plain DATABASE_URL into a SQLAlchemy async URL + connect args."""
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        keep = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            if k == "sslmode":
                if v in ("require", "verify-ca", "verify-full"):
                    connect_args["ssl"] = True
            elif k in ("channel_binding", "options"):
                continue
            else:
                keep.append((k, v))
        host = parts.hostname or ""
        if "-pooler" in host or "pgbouncer" in host:
            # PgBouncer in transaction mode does not support prepared statements
            connect_args["statement_cache_size"] = 0
            keep.append(("prepared_statement_cache_size", "0"))
        url = urlunsplit(parts._replace(query=urlencode(keep)))
    elif url.startswith("sqlite:///"):
        url = "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    return url, connect_args


class Database:
    def __init__(self, url: str, pool: str = "null"):
        self.url, connect_args = normalize_db_url(url)
        kwargs: dict = {"connect_args": connect_args}
        if self.url.startswith("sqlite"):
            path = self.url.split(":///", 1)[-1]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        elif pool == "queue":
            kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
        else:
            # short-lived connections: lets serverless Postgres (e.g. Neon) go to sleep when idle
            kwargs["poolclass"] = NullPool
        self.engine = create_async_engine(self.url, **kwargs)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self, retries: int = 6) -> None:
        last: Optional[BaseException] = None
        for i in range(retries):
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                return
            except Exception as e:  # cold start of serverless DBs can fail once
                last = e
                log.warning("database not ready (%s) - retry %s/%s", e, i + 1, retries)
                await asyncio.sleep(min(2 ** i, 15))
        assert last is not None
        raise last

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------ channels
    async def list_channels(self, active_only: bool = True) -> list:
        async with self.Session() as s:
            q = select(Channel).order_by(Channel.title)
            if active_only:
                q = q.where(Channel.active.is_(True))
            return list((await s.execute(q)).scalars())

    async def get_channel(self, cid: int) -> Optional[Channel]:
        async with self.Session() as s:
            return await s.get(Channel, cid)

    async def save_channel(self, cid: int, access_hash: int, title: str, username: Optional[str], added_by: int) -> Channel:
        async with self.Session() as s:
            ch = await s.get(Channel, cid)
            if ch is None:
                ch = Channel(id=cid, access_hash=access_hash, title=title, username=username, added_by=added_by, active=True)
                s.add(ch)
            else:
                ch.access_hash = access_hash or ch.access_hash
                ch.title = title or ch.title
                ch.username = username
                ch.active = True
            await s.commit()
            return ch

    async def touch_channel_hash(self, cid: int, access_hash: int) -> None:
        async with self.Session() as s:
            ch = await s.get(Channel, cid)
            if ch is not None and access_hash and ch.access_hash != access_hash:
                ch.access_hash = access_hash
                await s.commit()

    async def set_channel_active(self, cid: int, active: bool) -> None:
        async with self.Session() as s:
            ch = await s.get(Channel, cid)
            if ch is not None:
                ch.active = active
                await s.commit()

    # --------------------------------------------------------------------- posts
    async def create_post(self, **fields: Any) -> Post:
        async with self.Session() as s:
            p = Post(**fields)
            s.add(p)
            await s.commit()
            return p

    async def get_post(self, pid: int) -> Optional[Post]:
        async with self.Session() as s:
            return await s.get(Post, pid)

    async def update_post(self, pid: int, **fields: Any) -> Optional[Post]:
        async with self.Session() as s:
            p = await s.get(Post, pid)
            if p is None:
                return None
            for k, v in fields.items():
                setattr(p, k, v)
            p.updated_at = utcnow()
            await s.commit()
            return p

    async def delete_post(self, pid: int) -> None:
        async with self.Session() as s:
            p = await s.get(Post, pid)
            if p is not None:
                await s.delete(p)
                await s.commit()

    async def list_posts(self, channel_id: Optional[int], status: Optional[str], offset: int, limit: int) -> tuple:
        cond = []
        if channel_id is not None:
            cond.append(Post.channel_id == channel_id)
        if status:
            cond.append(Post.status == status)
        async with self.Session() as s:
            total = (await s.execute(select(func.count()).select_from(Post).where(*cond))).scalar_one()
            q = (
                select(Post)
                .where(*cond)
                .order_by(Post.status.asc(), desc(Post.message_id), desc(Post.id))
                .offset(offset)
                .limit(limit)
            )
            rows = list((await s.execute(q)).scalars())
            return rows, total

    async def latest_sent(self, channel_id: int, limit: int = 30) -> list:
        async with self.Session() as s:
            q = (
                select(Post)
                .where(Post.channel_id == channel_id, Post.status == "sent", Post.message_id.is_not(None))
                .order_by(desc(Post.message_id))
                .limit(limit)
            )
            return list((await s.execute(q)).scalars())

    async def sent_posts(self, channel_id: int) -> list:
        """Every published post of a channel, oldest first."""
        async with self.Session() as s:
            q = (
                select(Post)
                .where(Post.channel_id == channel_id, Post.status == "sent", Post.message_id.is_not(None))
                .order_by(Post.message_id)
            )
            return list((await s.execute(q)).scalars())

    async def sent_posts_from(self, channel_id: int, first_message_id: int) -> list:
        """Published posts of a channel from message id `first_message_id` on, oldest first."""
        async with self.Session() as s:
            q = (
                select(Post)
                .where(
                    Post.channel_id == channel_id,
                    Post.status == "sent",
                    Post.message_id.is_not(None),
                    Post.message_id >= first_message_id,
                )
                .order_by(Post.message_id, Post.id)
            )
            return list((await s.execute(q)).scalars())

    async def mine_message_ids(self, channel_id: int) -> set:
        async with self.Session() as s:
            q = select(Post.message_id).where(
                Post.channel_id == channel_id, Post.source == "bot", Post.message_id.is_not(None)
            )
            return {r for r in (await s.execute(q)).scalars()}

    async def find_by_message(self, channel_id: int, message_id: int) -> Optional[Post]:
        async with self.Session() as s:
            q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
            return (await s.execute(q)).scalar_one_or_none()

    async def forget_posts(self, channel_id: int, message_ids: list) -> int:
        """Remove the saved posts of these channel messages (they were deleted in the channel)."""
        if not message_ids:
            return 0
        async with self.Session() as s:
            rows = (
                await s.execute(select(Post).where(Post.channel_id == channel_id, Post.message_id.in_(list(message_ids))))
            ).scalars().all()
            for p in rows:
                await s.delete(p)
            await s.commit()
            return len(rows)

    async def posts_at(self, channel_id: int, message_ids: list) -> list:
        """The published posts that sit at these message ids."""
        if not message_ids:
            return []
        async with self.Session() as s:
            q = select(Post).where(
                Post.channel_id == channel_id, Post.status == "sent", Post.message_id.in_(list(message_ids))
            )
            return list((await s.execute(q)).scalars())

    async def release_slot(self, channel_id: int, message_id: int, keep_pid: Optional[int] = None) -> int:
        """Drop any other saved post at this message id (the sync may have picked the message up a moment earlier)."""
        async with self.Session() as s:
            q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
            if keep_pid is not None:
                q = q.where(Post.id != keep_pid)
            rows = list((await s.execute(q)).scalars())
            for p in rows:
                await s.delete(p)
            if rows:
                await s.commit()
            return len(rows)

    async def _drop_slots(self, s, channel_id: int, message_ids: list) -> None:
        """Inside a transaction: remove saved posts at these ids, so the posts about to be saved can take their place."""
        q = select(Post).where(Post.channel_id == channel_id, Post.message_id.in_(list(message_ids)))
        rows = list((await s.execute(q)).scalars())
        for p in rows:
            await s.delete(p)
        if rows:
            await s.flush()

    # ------------------------------------------------------- what happens outside the bot
    async def get_sync(self, channel_id: int) -> Optional[ChannelSync]:
        async with self.Session() as s:
            return await s.get(ChannelSync, channel_id)

    async def max_saved_id(self, channel_id: int) -> int:
        """The newest message id among the channel's published posts (0 if there is none)."""
        async with self.Session() as s:
            q = select(func.max(Post.message_id)).where(
                Post.channel_id == channel_id, Post.status == "sent", Post.message_id.is_not(None)
            )
            return int((await s.execute(q)).scalar_one_or_none() or 0)

    async def save_sync(
        self, channel_id: int, *, last_top: int, last_pts: int, deferred: int = 0, deep: bool = False
    ) -> None:
        async with self.Session() as s:
            row = await s.get(ChannelSync, channel_id)
            if row is None:
                row = ChannelSync(channel_id=channel_id)
                s.add(row)
            now = utcnow()
            row.last_top, row.last_pts, row.deferred, row.last_run = last_top, last_pts, deferred, now
            if deep:
                row.last_deep = now
            await s.commit()

    async def adopt_message(self, channel_id: int, message_id: int, fields: dict, sent_at: Optional[datetime] = None) -> tuple:
        """Save a post somebody else made in the channel. Returns (post, True), or (None, False) when the bot already
        has a post at that message id (also when another task saved it a moment ago)."""
        async with self.Session() as s:
            q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
            if (await s.execute(q)).scalar_one_or_none() is not None:
                return None, False
            p = Post(channel_id=channel_id, message_id=message_id, status="sent", source="adopted", created_by=0,
                     sent_at=sent_at or utcnow(), **fields)
            s.add(p)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None, False
            return p, True

    async def sync_update_post(self, pid: int, expected: Optional[datetime], fields: dict) -> bool:
        """Save what the channel shows - but only if the saved post is still exactly as it was when it was read
        (`expected` is its updated_at then), so a change made through the bot meanwhile is never overwritten."""
        async with self.Session() as s:
            res = await s.execute(
                update(Post).where(Post.id == pid, Post.updated_at == expected).values(**fields, updated_at=utcnow())
            )
            await s.commit()
            return bool(res.rowcount)

    # ------------------------------------------------------------ timed deletions
    async def schedule_deletes(self, rows: list) -> None:
        """rows: dicts with channel_id, message_id, delete_at, group_id, created_by."""
        if not rows:
            return
        async with self.Session() as s:
            s.add_all([ScheduledDelete(**r) for r in rows])
            await s.commit()

    async def due_deletes(self, now: datetime, limit: int = 500) -> list:
        async with self.Session() as s:
            q = (
                select(ScheduledDelete)
                .where(ScheduledDelete.status == "pending", ScheduledDelete.delete_at <= now)
                .order_by(ScheduledDelete.delete_at, ScheduledDelete.id)
                .limit(limit)
            )
            return list((await s.execute(q)).scalars())

    async def next_delete_at(self) -> Optional[datetime]:
        async with self.Session() as s:
            q = select(func.min(ScheduledDelete.delete_at)).where(ScheduledDelete.status == "pending")
            return as_utc((await s.execute(q)).scalar_one_or_none())

    async def pending_deletes(self, group_id: Optional[str] = None, status: str = "pending") -> list:
        async with self.Session() as s:
            q = select(ScheduledDelete).where(ScheduledDelete.status == status).order_by(ScheduledDelete.id)
            if group_id:
                q = q.where(ScheduledDelete.group_id == group_id)
            return list((await s.execute(q)).scalars())

    async def finish_deletes(self, ids: list) -> None:
        if not ids:
            return
        async with self.Session() as s:
            for row in (await s.execute(select(ScheduledDelete).where(ScheduledDelete.id.in_(list(ids))))).scalars():
                await s.delete(row)
            await s.commit()

    async def retry_delete(self, sid: int, *, at: datetime, error: str) -> None:
        async with self.Session() as s:
            row = await s.get(ScheduledDelete, sid)
            if row is not None:
                row.attempts += 1
                row.delete_at = at
                row.last_error = error[:160]
                await s.commit()

    async def fail_delete(self, sid: int, error: str) -> None:
        async with self.Session() as s:
            row = await s.get(ScheduledDelete, sid)
            if row is not None:
                row.attempts += 1
                row.status = "failed"
                row.last_error = error[:160]
                await s.commit()

    # ------------------------------------------------------------- replace history
    async def create_batch(self, batch_id: str, old: str, new: str, user_id: int) -> None:
        async with self.Session() as s:
            s.add(Batch(id=batch_id, old_username=old, new_username=new, created_by=user_id))
            await s.commit()

    async def _adopt(self, s, channel_id: int, message_id: int, fields: dict, user_id: int) -> None:
        q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
        p = (await s.execute(q)).scalar_one_or_none()
        if p is not None:
            for k, v in fields.items():
                setattr(p, k, v)
            p.updated_at = utcnow()
        else:
            s.add(
                Post(
                    channel_id=channel_id,
                    message_id=message_id,
                    status="sent",
                    source="adopted",
                    created_by=user_id,
                    sent_at=utcnow(),
                    **fields,
                )
            )

    async def record_changes(self, batch_id: str, channel_id: int, records: list, user_id: int) -> None:
        """records: [{message_id, old_state, new_state, adopt: {post fields}}]"""
        if not records:
            return
        async with self.Session() as s:
            for r in records:
                s.add(
                    Change(
                        batch_id=batch_id,
                        channel_id=channel_id,
                        message_id=r["message_id"],
                        old_state=r["old_state"],
                        new_state=r["new_state"],
                    )
                )
                await self._adopt(s, channel_id, r["message_id"], r["adopt"], user_id)
            await s.commit()

    async def last_batch(self) -> Optional[Batch]:
        async with self.Session() as s:
            q = select(Batch).where(Batch.undone.is_(False)).order_by(desc(Batch.created_at)).limit(1)
            return (await s.execute(q)).scalar_one_or_none()

    async def get_batch(self, batch_id: str) -> Optional[Batch]:
        async with self.Session() as s:
            return await s.get(Batch, batch_id)

    async def batch_changes(self, batch_id: str) -> list:
        async with self.Session() as s:
            q = select(Change).where(Change.batch_id == batch_id, Change.reverted.is_(False)).order_by(Change.id)
            return list((await s.execute(q)).scalars())

    async def count_batch_changes(self, batch_id: str) -> int:
        async with self.Session() as s:
            q = select(func.count()).select_from(Change).where(Change.batch_id == batch_id, Change.reverted.is_(False))
            return (await s.execute(q)).scalar_one()

    async def finish_undo(self, batch_id: str, change_ids: list, restored: list, all_done: bool) -> None:
        """restored: [(channel_id, message_id, state)] -> refresh the saved post copies."""
        async with self.Session() as s:
            for cid in change_ids:
                c = await s.get(Change, cid)
                if c is not None:
                    c.reverted = True
            for channel_id, message_id, state in restored:
                q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
                p = (await s.execute(q)).scalar_one_or_none()
                if p is not None:
                    p.text = state.get("text", "")
                    p.entities = state.get("entities", [])
                    p.buttons = state.get("buttons", [])
                    p.updated_at = utcnow()
            if all_done:
                b = await s.get(Batch, batch_id)
                if b is not None:
                    b.undone = True
            await s.commit()


    # ------------------------------------------------------------------ /repost
    OPEN_STATES = ("copying", "stopped", "incomplete", "copied")

    async def create_migration(self, mid: str, channel_id: int, *, old, new, include_typed: bool, include_posts: bool,
                               first_id: int, last_id: int, partial: bool, user_id: int) -> None:
        async with self.Session() as s:
            s.add(Migration(id=mid, channel_id=channel_id, old_username=old, new_username=new,
                            include_typed=include_typed, include_posts=include_posts, first_id=first_id,
                            last_id=last_id, partial=partial, created_by=user_id))
            await s.commit()

    async def get_migration(self, mid: str) -> Optional[Migration]:
        async with self.Session() as s:
            return await s.get(Migration, mid)

    async def open_migration(self, channel_id: Optional[int] = None) -> Optional[Migration]:
        """The newest repost that is not finished yet (copies exist, old posts not deleted / copies not removed)."""
        async with self.Session() as s:
            q = select(Migration).where(Migration.status.in_(self.OPEN_STATES))
            if channel_id is not None:
                q = q.where(Migration.channel_id == channel_id)
            q = q.order_by(desc(Migration.created_at)).limit(1)
            return (await s.execute(q)).scalar_one_or_none()

    async def set_migration_status(self, mid: str, status: str) -> None:
        async with self.Session() as s:
            m = await s.get(Migration, mid)
            if m is not None:
                m.status = status
                await s.commit()

    async def migration_done_old_ids(self, mid: str) -> set:
        async with self.Session() as s:
            q = select(MigrationItem.old_id).where(MigrationItem.migration_id == mid)
            return set((await s.execute(q)).scalars())

    async def record_repost(self, mid: str, channel_id: int, rows: list, user_id: int) -> None:
        """rows: [{old_id, new_id, post: {Post fields}}] - saves the mapping and registers the new posts."""
        if not rows:
            return
        async with self.Session() as s:
            await self._drop_slots(s, channel_id, [r["new_id"] for r in rows])
            for r in rows:
                s.add(MigrationItem(migration_id=mid, old_id=r["old_id"], new_id=r["new_id"]))
                s.add(Post(channel_id=channel_id, message_id=r["new_id"], status="sent", source="bot",
                           created_by=user_id, sent_at=utcnow(), **r["post"]))
            await s.commit()

    async def migration_pairs(
        self, mid: Optional[str] = None, channel_id: Optional[int] = None, alive_only: bool = True,
        only_deleted_old: bool = False,
    ) -> list:
        """[(old id, new id)] of the copies of one repost (`mid`) or of every repost of a channel, oldest repost first.
        alive_only: leave out copies that were removed again (undo). only_deleted_old: only posts whose original
        has been deleted (links to those are dead)."""
        async with self.Session() as s:
            q = select(MigrationItem.old_id, MigrationItem.new_id).join(Migration, Migration.id == MigrationItem.migration_id)
            if mid is not None:
                q = q.where(MigrationItem.migration_id == mid)
            if channel_id is not None:
                q = q.where(Migration.channel_id == channel_id)
            if alive_only:
                q = q.where(MigrationItem.new_deleted.is_(False))
            if only_deleted_old:
                q = q.where(MigrationItem.old_deleted.is_(True))
            q = q.order_by(Migration.created_at, MigrationItem.id)
            return [(a, b) for a, b in (await s.execute(q)).all()]

    async def update_post_by_message(self, channel_id: int, message_id: int, **fields: Any) -> Optional[Post]:
        """Change the saved copy of the post that sits at `message_id` (None if the bot does not know that post)."""
        async with self.Session() as s:
            q = select(Post).where(Post.channel_id == channel_id, Post.message_id == message_id)
            p = (await s.execute(q)).scalar_one_or_none()
            if p is None:
                return None
            for k, v in fields.items():
                setattr(p, k, v)
            p.updated_at = utcnow()
            await s.commit()
            return p

    async def migration_pending_ids(self, mid: str, which: str, limit: int = 100) -> list:
        col, flag = (MigrationItem.old_id, MigrationItem.old_deleted) if which == "old" else (MigrationItem.new_id, MigrationItem.new_deleted)
        async with self.Session() as s:
            q = select(col).where(MigrationItem.migration_id == mid, flag.is_(False)).order_by(col).limit(limit)
            return list((await s.execute(q)).scalars())

    async def mark_migration_deleted(self, mid: str, channel_id: int, which: str, ids: list) -> None:
        """The messages are gone from Telegram: flag them and drop their saved post copies."""
        if not ids:
            return
        col, flag = ("old_id", "old_deleted") if which == "old" else ("new_id", "new_deleted")
        async with self.Session() as s:
            q = select(MigrationItem).where(MigrationItem.migration_id == mid, getattr(MigrationItem, col).in_(ids))
            for it in (await s.execute(q)).scalars():
                setattr(it, flag, True)
            pq = select(Post).where(Post.channel_id == channel_id, Post.message_id.in_(ids))
            for p in (await s.execute(pq)).scalars():
                await s.delete(p)
            await s.commit()

    async def migration_counts(self, mid: str) -> dict:
        async with self.Session() as s:
            total = (await s.execute(select(func.count()).select_from(MigrationItem).where(MigrationItem.migration_id == mid))).scalar_one()
            old_left = (await s.execute(select(func.count()).select_from(MigrationItem).where(
                MigrationItem.migration_id == mid, MigrationItem.old_deleted.is_(False)))).scalar_one()
            new_left = (await s.execute(select(func.count()).select_from(MigrationItem).where(
                MigrationItem.migration_id == mid, MigrationItem.new_deleted.is_(False)))).scalar_one()
            lo = (await s.execute(select(func.min(MigrationItem.new_id)).where(MigrationItem.migration_id == mid))).scalar_one()
            hi = (await s.execute(select(func.max(MigrationItem.new_id)).where(MigrationItem.migration_id == mid))).scalar_one()
            return {"copied": total, "old_left": old_left, "new_left": new_left, "first_new": lo, "last_new": hi}

    # ------------------------------------------------------------------- /shift
    SHIFT_OPEN = ("copying", "stopped", "incomplete")

    async def create_shift(self, sid: str, *, src, dst_channel_id: int, first_id: int, last_id: int, user_id: int) -> None:
        """`src`: any object with id, access_hash, title and username (the source channel need not be registered)."""
        async with self.Session() as s:
            s.add(Shift(id=sid, src_channel_id=src.id, src_access_hash=src.access_hash or 0, src_title=src.title or "",
                        src_username=src.username, dst_channel_id=dst_channel_id, first_id=first_id, last_id=last_id,
                        created_by=user_id))
            await s.commit()

    async def get_shift(self, sid: str) -> Optional[Shift]:
        async with self.Session() as s:
            return await s.get(Shift, sid)

    async def open_shift(self, src_id: Optional[int] = None, dst_id: Optional[int] = None) -> Optional[Shift]:
        """The newest shift that is not finished (still copying, stopped, or incomplete)."""
        async with self.Session() as s:
            q = select(Shift).where(Shift.status.in_(self.SHIFT_OPEN))
            if src_id is not None:
                q = q.where(Shift.src_channel_id == src_id)
            if dst_id is not None:
                q = q.where(Shift.dst_channel_id == dst_id)
            return (await s.execute(q.order_by(desc(Shift.created_at)).limit(1))).scalar_one_or_none()

    async def set_shift_status(self, sid: str, status: str) -> None:
        async with self.Session() as s:
            sh = await s.get(Shift, sid)
            if sh is not None:
                sh.status = status
                await s.commit()

    async def shift_done_ids(self, sid: str) -> set:
        async with self.Session() as s:
            q = select(ShiftItem.old_id).where(ShiftItem.shift_id == sid)
            return set((await s.execute(q)).scalars())

    async def shift_pairs(self, sid: str, alive_only: bool = True) -> list:
        """[(source id, id of the copy)] of one shift, in copy order."""
        async with self.Session() as s:
            q = select(ShiftItem.old_id, ShiftItem.new_id).where(ShiftItem.shift_id == sid)
            if alive_only:
                q = q.where(ShiftItem.new_deleted.is_(False))
            return [(a, b) for a, b in (await s.execute(q.order_by(ShiftItem.id))).all()]

    async def shifted_ids(self, src_id: int, dst_id: int, first: int, last: int) -> set:
        """Source ids in first..last that already have a live copy in `dst_id` from an earlier shift."""
        async with self.Session() as s:
            q = (
                select(ShiftItem.old_id)
                .join(Shift, Shift.id == ShiftItem.shift_id)
                .where(Shift.src_channel_id == src_id, Shift.dst_channel_id == dst_id, ShiftItem.new_deleted.is_(False),
                       ShiftItem.old_id >= first, ShiftItem.old_id <= last)
            )
            return set((await s.execute(q)).scalars())

    async def record_shift(self, sid: str, dst_channel_id: int, rows: list, user_id: int) -> None:
        """rows: [{old_id, new_id, post: {Post fields}}] - saves the mapping and registers the copies in My posts."""
        if not rows:
            return
        async with self.Session() as s:
            await self._drop_slots(s, dst_channel_id, [r["new_id"] for r in rows])
            for r in rows:
                s.add(ShiftItem(shift_id=sid, old_id=r["old_id"], new_id=r["new_id"]))
                s.add(Post(channel_id=dst_channel_id, message_id=r["new_id"], status="sent", source="bot",
                           created_by=user_id, sent_at=utcnow(), **r["post"]))
            await s.commit()

    async def shift_counts(self, sid: str) -> dict:
        async with self.Session() as s:
            base = ShiftItem.shift_id == sid
            total = (await s.execute(select(func.count()).select_from(ShiftItem).where(base))).scalar_one()
            left = (await s.execute(select(func.count()).select_from(ShiftItem).where(base, ShiftItem.new_deleted.is_(False)))).scalar_one()
            lo = (await s.execute(select(func.min(ShiftItem.new_id)).where(base))).scalar_one()
            hi = (await s.execute(select(func.max(ShiftItem.new_id)).where(base))).scalar_one()
            return {"copied": total, "new_left": left, "first_new": lo, "last_new": hi}

    async def shift_pending_new(self, sid: str, limit: int = 100) -> list:
        async with self.Session() as s:
            q = select(ShiftItem.new_id).where(ShiftItem.shift_id == sid, ShiftItem.new_deleted.is_(False)).order_by(ShiftItem.new_id).limit(limit)
            return list((await s.execute(q)).scalars())

    async def mark_shift_deleted(self, sid: str, dst_channel_id: int, ids: list) -> None:
        """The copies are gone from Telegram: flag them and drop their saved posts."""
        if not ids:
            return
        async with self.Session() as s:
            q = select(ShiftItem).where(ShiftItem.shift_id == sid, ShiftItem.new_id.in_(ids))
            for it in (await s.execute(q)).scalars():
                it.new_deleted = True
            pq = select(Post).where(Post.channel_id == dst_channel_id, Post.message_id.in_(ids))
            for p in (await s.execute(pq)).scalars():
                await s.delete(p)
            await s.commit()

    # -------------------------------------------------------------------- export
    async def export_all(self) -> dict:
        def clean(d: dict) -> dict:
            return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}

        async with self.Session() as s:
            chans = list((await s.execute(select(Channel))).scalars())
            posts = list((await s.execute(select(Post).order_by(Post.id))).scalars())
            return {
                "exported_at": utcnow().isoformat(),
                "channels": [
                    clean({c.name: getattr(ch, c.name) for c in ch.__table__.columns}) for ch in chans
                ],
                "posts": [clean(p.as_dict()) for p in posts],
            }
