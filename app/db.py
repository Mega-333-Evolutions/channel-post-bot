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
)
from sqlalchemy.dialects.postgresql import JSONB
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
            for r in rows:
                s.add(MigrationItem(migration_id=mid, old_id=r["old_id"], new_id=r["new_id"]))
                s.add(Post(channel_id=channel_id, message_id=r["new_id"], status="sent", source="bot",
                           created_by=user_id, sent_at=utcnow(), **r["post"]))
            await s.commit()

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
