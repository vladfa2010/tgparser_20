#!/usr/bin/env python3
"""
TG Parser v2 — Telegram channel parser.

v2 changes vs v1:
  - Channel list comes from the DB (channels.is_active), managed in /admin.
    Env CHANNELS remains a first-run seed fallback.
  - Upsert instead of N+1 duplicate SELECTs: INSERT ... ON CONFLICT
    (channel_id, telegram_message_id) DO UPDATE — every run refreshes
    views/forwards/replies and overwrites edited posts (edit_date).
  - Per-channel run mode:
      * backfill_full flag (admin «Докачать всё») → full history, flag cleared on success;
      * no posts yet for the channel → last 2000 messages (first import);
      * otherwise → sliding window of the last 500 messages (update mode).
  - HISTORY=1 env keeps v1 behavior: full history for all channels (one-off backfill).
"""
import asyncio
import hashlib
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message

from db import Channel, ParseLog, Post, async_session, ensure_schema

load_dotenv()

# ─── Config ──────────────────────────────────────────────────
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_STRING_SESSION = os.getenv("TG_STRING_SESSION", "")
TG_SESSION = os.getenv("TG_SESSION", "/app/sessions/markettwits_session")
# Fallback/first-run seed only: the live source of truth is the DB (see /admin).
_CHANNELS_ENV = os.getenv("CHANNELS", os.getenv("CHANNEL_USERNAME", "markettwits"))
CHANNELS = [c.strip() for c in _CHANNELS_ENV.split(",") if c.strip()]
SCHEDULE_MODE = os.getenv("SCHEDULE_MODE", "0") == "1"
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "300"))
HISTORY = os.getenv("HISTORY", "0") == "1"
LIMIT = os.getenv("LIMIT")
LIMIT = int(LIMIT) if LIMIT else None

# Sliding window sizes (ТЗ-tgparser-01, Задача 3)
WINDOW_SIZE = 500          # обновление счётчиков/правок каждый прогон
FIRST_IMPORT_SIZE = 2000   # первичная докачка нового канала
FLUSH_EVERY = 200          # пакет upsert-ов между commit'ами


def _validate():
    if not TG_API_ID or TG_API_ID == 0:
        print("ERROR: Set TG_API_ID env var (get it from https://my.telegram.org/apps)")
        sys.exit(1)
    if not TG_API_HASH:
        print("ERROR: Set TG_API_HASH env var")
        sys.exit(1)
    if not TG_STRING_SESSION and not os.path.exists(TG_SESSION + ".session"):
        print("ERROR: Either TG_STRING_SESSION env var or file session required.")
        print("Run 'python generate_session.py' locally to get a string session.")
        sys.exit(1)


# ─── Helpers ─────────────────────────────────────────────────
def extract_hashtags(text_: str) -> list:
    # #\w+ only catches Latin; #\S+ catches everything after # until whitespace
    return re.findall(r'#\S+', text_) if text_ else []


def extract_mentions(text_: str) -> list:
    return re.findall(r'@\w+', text_) if text_ else []


def extract_urls(text_: str) -> list:
    return re.findall(r'https?://[^\s]+', text_) if text_ else []


def make_text_hash(text_: str) -> str:
    return hashlib.sha256(text_.encode()).hexdigest() if text_ else ""


def get_media_type(message: Message) -> Optional[str]:
    if not message.media:
        return None
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    return "other"


def _normalize_username(u: str) -> str:
    return u.strip().lstrip("@").lower()


# ─── Parser ──────────────────────────────────────────────────
class MarketTwitsParser:
    def __init__(self):
        if TG_STRING_SESSION:
            session = StringSession(TG_STRING_SESSION)
        else:
            from telethon.sessions import SQLiteSession
            os.makedirs(os.path.dirname(TG_SESSION) or ".", exist_ok=True)
            session = TG_SESSION
        self.client = TelegramClient(session, TG_API_ID, TG_API_HASH)

    async def init_db(self):
        await ensure_schema()

    # ─── Channel list: DB first, env as first-run seed ───────
    async def get_channels(self, session) -> list[Channel]:
        rows = (await session.execute(
            select(Channel).where(Channel.is_active).order_by(Channel.username)
        )).scalars().all()
        if rows:
            return rows
        # Пустая таблица → сидим из env (первый запуск / локалка)
        if not CHANNELS:
            return []
        feed_map: dict[str, str] = {}
        for feed_type, env_name in (("news", "NEWS_CHANNELS"), ("analytics", "ANALYTICS_CHANNELS")):
            for u in os.getenv(env_name, "").split(","):
                u = _normalize_username(u)
                if u:
                    feed_map[u] = feed_type
        for username in CHANNELS:
            session.add(Channel(
                username=_normalize_username(username),
                is_active=True,
                feed_type=feed_map.get(_normalize_username(username)),
                title="@" + _normalize_username(username),
            ))
        await session.flush()
        print(f"Seeded {len(CHANNELS)} channel(s) from env into DB")
        return (await session.execute(
            select(Channel).where(Channel.is_active).order_by(Channel.username)
        )).scalars().all()

    async def sync_channel(self, session, channel: Channel) -> None:
        """Refresh telegram metadata; admin-added rows have telegram_id=None."""
        entity = await self.client.get_entity(channel.username)
        channel.telegram_id = entity.id
        channel.title = entity.title
        channel.description = getattr(entity, "about", None)
        channel.subscriber_count = getattr(entity, "participants_count", 0) or 0

    def _message_values(self, channel_db_id: int, message: Message) -> dict:
        text_ = message.text or ""
        forward_from = None
        if message.forward and message.forward.chat:
            forward_from = message.forward.chat.username or message.forward.chat.title
        return dict(
            channel_id=channel_db_id,
            telegram_message_id=message.id,
            text=text_,
            text_hash=make_text_hash(text_),
            views_count=message.views or 0,
            forwards_count=message.forwards or 0,
            replies_count=message.replies.replies if message.replies else 0,
            hashtags=extract_hashtags(text_),
            mentions=extract_mentions(text_),
            urls=extract_urls(text_),
            forward_from=forward_from,
            has_media=message.media is not None,
            media_type=get_media_type(message),
            published_at=message.date,
            edited_at=message.edit_date,
        )

    def _upsert_stmt(self, values_list: list):
        stmt = pg_insert(Post).values(values_list)
        # Перезаписываем только изменяемые поля; published_at/created_at — от первой вставки.
        return stmt.on_conflict_do_update(
            index_elements=["channel_id", "telegram_message_id"],
            set_={
                "text": stmt.excluded.text,
                "text_hash": stmt.excluded.text_hash,
                "views_count": stmt.excluded.views_count,
                "forwards_count": stmt.excluded.forwards_count,
                "replies_count": stmt.excluded.replies_count,
                "hashtags": stmt.excluded.hashtags,
                "mentions": stmt.excluded.mentions,
                "urls": stmt.excluded.urls,
                "forward_from": stmt.excluded.forward_from,
                "has_media": stmt.excluded.has_media,
                "media_type": stmt.excluded.media_type,
                "edited_at": stmt.excluded.edited_at,
            },
        )

    async def parse_channel(self, session, channel: Channel, history: bool = False):
        """Parse a single channel: upsert window / full history depending on mode."""
        # Перечитываем канал в текущей сессии (изменения флага backfill должны сохраняться)
        channel = await session.get(Channel, channel.id)
        await self.sync_channel(session, channel)

        post_count = (await session.execute(
            select(func.count()).select_from(Post).where(Post.channel_id == channel.id)
        )).scalar() or 0

        if history:
            window, mode = None, "HISTORY (полная история)"
        elif channel.backfill_full:
            window, mode = None, "BACKFILL (полная история по флагу)"
        elif post_count == 0:
            window, mode = FIRST_IMPORT_SIZE, f"FIRST IMPORT (последние {FIRST_IMPORT_SIZE})"
        else:
            window, mode = WINDOW_SIZE, f"UPDATE (окно {WINDOW_SIZE})"
        if LIMIT:
            window, mode = LIMIT, f"LIMIT={LIMIT}"

        entity = await self.client.get_entity(channel.username)
        print(f"  [{channel.username}] mode={mode}, stored posts={post_count}")

        parsed_count = 0
        new_count = 0
        pending: list[dict] = []

        async def flush():
            nonlocal new_count
            if not pending:
                return
            res = await session.execute(
                self._upsert_stmt(pending).returning(text("(xmax = 0) AS was_inserted"))
            )
            new_count += sum(1 for r in res.fetchall() if r[0])
            pending.clear()
            await session.commit()

        async for message in self.client.iter_messages(entity, limit=window):
            if not message.text and not message.media:
                continue
            parsed_count += 1
            pending.append(self._message_values(channel.id, message))
            if len(pending) >= FLUSH_EVERY:
                await flush()

        await flush()

        channel.last_parsed_at = datetime.utcnow()
        if channel.backfill_full and parsed_count > 0:
            channel.backfill_full = False
            print(f"  [{channel.username}] backfill done, flag cleared")

        return parsed_count, new_count

    async def parse(self, history: bool = False):
        """Parse all active channels from the DB."""
        start_time = time.time()
        total_parsed = 0
        total_new = 0

        async with async_session() as session:
            channels = await self.get_channels(session)
            await session.commit()

        if not channels:
            print("No active channels in DB and CHANNELS env is empty — nothing to do.")
            return

        for channel in channels:
            print(f"\n{'='*50}")
            print(f"Parsing channel: @{channel.username}")
            print(f"{'='*50}")

            log = ParseLog(started_at=datetime.utcnow(), channel_id=channel.id)

            async with async_session() as session:
                parsed, new = 0, 0
                try:
                    parsed, new = await self.parse_channel(session, channel, history=history)
                    total_parsed += parsed
                    total_new += new
                    log.posts_parsed = parsed
                    log.posts_new = new
                except Exception as e:
                    log.error_message = f"[{channel.username}] {str(e)}"
                    print(f"ERROR parsing {channel.username}: {e}")
                    # Прерывание бэкфилла НЕ снимает флаг — докачаем с места обрыва
                finally:
                    log.duration_ms = int((time.time() - start_time) * 1000)
                    log.finished_at = datetime.utcnow()
                    session.add(log)
                    await session.commit()
                    print(f"[{channel.username}] Done! Parsed: {parsed}, New: {new}")

        print(
            f"\n{'='*50}\n"
            f"ALL CHANNELS DONE! Total parsed: {total_parsed}, "
            f"Total new: {total_new}, Time: {int((time.time() - start_time) * 1000)}ms\n"
            f"{'='*50}"
        )

    async def run_once(self):
        _validate()
        await self.init_db()
        await self.client.start()
        me = await self.client.get_me()
        print(f"Connected as {me.first_name} (@{me.username})")
        try:
            await self.parse(history=HISTORY)
        finally:
            await self.client.disconnect()

    async def run_scheduled(self):
        _validate()
        await self.init_db()
        await self.client.start()
        me = await self.client.get_me()
        print(f"Scheduler connected as {me.first_name}")
        print(f"Parsing every {INTERVAL_SEC} seconds")

        try:
            while True:
                print(f"\n[{datetime.utcnow().isoformat()}] Starting parse...")
                try:
                    await self.parse(history=False)
                except Exception as e:
                    print(f"Parse error: {e}")
                print(f"Sleeping {INTERVAL_SEC}s...")
                await asyncio.sleep(INTERVAL_SEC)
        finally:
            await self.client.disconnect()


async def main():
    parser = MarketTwitsParser()
    if SCHEDULE_MODE:
        await parser.run_scheduled()
    else:
        await parser.run_once()


if __name__ == "__main__":
    asyncio.run(main())
