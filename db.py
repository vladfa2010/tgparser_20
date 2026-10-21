#!/usr/bin/env python3
"""
TG Parser v2 — Database layer.

Shared by web.py (FastAPI), admin.py, auth.py and markettwits_parser.py:
engine, session factory, models, lightweight migrations (no alembic).

Migrations run at web startup: new columns/tables use IF NOT EXISTS, so a
database created by the v1 parser keeps working and gets upgraded in place.
"""
import os
from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, DateTime, Integer, String, Text,
    UniqueConstraint, select, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# Render дает postgres://... — SQLAlchemy async требует postgresql+asyncpg://
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

Base = declarative_base()
engine = create_async_engine(DATABASE_URL) if DATABASE_URL else None
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else None


class FeedGroup(Base):
    """Группа каналов → отдельный RSS-поток /rss/{slug}."""
    __tablename__ = "feed_groups"
    id = Column(Integer, primary_key=True)
    slug = Column(String(200), unique=True, index=True)
    title = Column(String(300))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True)
    username = Column(String(255), unique=True, index=True)
    group_id = Column(Integer, index=True, nullable=True)  # FeedGroup.id | NULL (вне группы)
    title = Column(String(500))
    description = Column(Text)
    subscriber_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    feed_type = Column(String(20), nullable=True)      # 'news' | 'analytics' | NULL
    backfill_full = Column(Boolean, default=False)     # «докачать всю историю»
    rss_title = Column(String(500), nullable=True)     # кастомное название в RSS (приоритет над title из TG)
    last_parsed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, index=True)
    telegram_message_id = Column(BigInteger)
    text = Column(Text)
    text_hash = Column(String(64), index=True)
    views_count = Column(Integer, default=0)
    forwards_count = Column(Integer, default=0)
    replies_count = Column(Integer, default=0)
    hashtags = Column(JSON, default=list)
    mentions = Column(JSON, default=list)
    urls = Column(JSON, default=list)
    forward_from = Column(String(255))
    has_media = Column(Boolean, default=False)
    media_type = Column(String(50))
    published_at = Column(DateTime(timezone=True))
    edited_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("channel_id", "telegram_message_id"),)


class ParseLog(Base):
    __tablename__ = "parse_logs"
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer)
    posts_parsed = Column(Integer, default=0)
    posts_new = Column(Integer, default=0)
    duration_ms = Column(Integer)
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))


class Settings(Base):
    """Key-value storage: admin credentials, session version, feed titles."""
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)


# ─── Migrations (idempotent, safe to run on every startup) ──
MIGRATION_DDL = [
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS feed_type VARCHAR(20)",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS backfill_full BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS rss_title VARCHAR(500)",
    """
    CREATE TABLE IF NOT EXISTS feed_groups (
        id SERIAL PRIMARY KEY,
        slug VARCHAR(200) UNIQUE,
        title VARCHAR(300),
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS group_id INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_channels_group ON channels (group_id)",
    "CREATE INDEX IF NOT EXISTS ix_posts_published_at ON posts (published_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_posts_channel_pub ON posts (channel_id, published_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        key VARCHAR(100) PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
]


async def ensure_schema():
    """create_all for fresh DBs + additive migrations for existing v1 DBs."""
    if engine is None:
        raise RuntimeError("DATABASE_URL is not set")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for ddl in MIGRATION_DDL:
            await conn.execute(text(ddl))


# ─── Settings helpers ────────────────────────────────────────
async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = (await session.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    return row.value if row and row.value is not None else default


async def set_setting(session: AsyncSession, key: str, value: str):
    row = (await session.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if row:
        row.value = value
    else:
        session.add(Settings(key=key, value=value))


# ─── feed_type (v2.0) → feed_groups ──────────────────────────
DEFAULT_GROUP_TITLES = {"news": "Новости", "analytics": "Аналитика"}


async def migrate_groups():
    """Переносит разметку feed_type на группы. Идемпотентно: для каждого канала
    с feed_type создаёт (или находит) группу с этим slug и назначает group_id;
    назначенный feed_type очищается. Название группы — из settings, иначе дефолт."""
    async with async_session() as s:
        rows = (await s.execute(
            select(Channel.id, Channel.feed_type).where(Channel.feed_type.isnot(None))
        )).all()
        for ch_id, ft in rows:
            group = (await s.execute(select(FeedGroup).where(FeedGroup.slug == ft))).scalar_one_or_none()
            if not group:
                custom = await get_setting(s, f"rss_{ft}_title", "")
                group = FeedGroup(slug=ft, title=custom or DEFAULT_GROUP_TITLES.get(ft) or ft)
                s.add(group)
                await s.flush()
            ch = await s.get(Channel, ch_id)
            ch.group_id = group.id
            ch.feed_type = None
        if rows:
            await s.commit()
