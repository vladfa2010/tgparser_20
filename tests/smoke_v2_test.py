"""Smoke-тест v2.0 по критериям ТЗ-tgparser-01 + персональные фиды.

Нужен локальный PostgreSQL. Запуск:
    DATABASE_URL=postgresql+asyncpg://user@localhost:5432/tgparser_v2_test \
    ADMIN_PASSWORD=test_password_123 python3 tests/smoke_v2_test.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.getenv("DATABASE_URL"):
    sys.exit("Задайте DATABASE_URL (тестовая база будет очищена!)")
os.environ.setdefault("JWT_SECRET", "test_secret_" + "0" * 32)
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test_password_123")
os.environ.setdefault("CHANNELS", "testnews,testanalytics,unassigned")

import auth  # noqa: E402
import httpx  # noqa: E402
from db import Channel, ParseLog, Post, Settings, async_session, ensure_schema  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from web import app  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


async def wipe_and_seed():
    await ensure_schema()
    async with async_session() as s:
        await s.execute(delete(Post))
        await s.execute(delete(ParseLog))
        await s.execute(delete(Channel))
        await s.execute(delete(Settings))
        await s.commit()
        ch_news = Channel(username="testnews", title="Тестовые Новости", is_active=True, feed_type="news", telegram_id=111)
        ch_ana = Channel(username="testanalytics", title="Тестовая Аналитика", is_active=True, feed_type="analytics", telegram_id=222)
        ch_none = Channel(username="unassigned", title="Без потока", is_active=True, feed_type=None, telegram_id=333)
        s.add_all([ch_news, ch_ana, ch_none])
        await s.flush()
        now = datetime.now(timezone.utc)
        s.add(Post(channel_id=ch_news.id, telegram_message_id=1, text="Новость: рост рынка <b>MOEX</b> & дивиденды", views_count=100, hashtags=["#рост"], published_at=now))
        s.add(Post(channel_id=ch_ana.id, telegram_message_id=1, text="Аналитика: цель по Сберу", views_count=50, hashtags=["#сбер"], published_at=now))
        s.add(Post(channel_id=ch_none.id, telegram_message_id=1, text="Пост без потока", views_count=10, published_at=now))
        await s.commit()
        return {"testnews": ch_news.id, "testanalytics": ch_ana.id, "unassigned": ch_none.id}


async def main():
    ids = await wipe_and_seed()
    await auth.ensure_admin()
    auth._attempts.clear()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        r = await client.get("/health")
        check("C17 /health 200 + version=2.0.0", r.status_code == 200 and r.json().get("version") == "2.0.0", r.text[:80])

        r = await client.get("/")
        check("C3 GET / → redirect /login", r.status_code in (301, 302, 307) and "/login" in r.headers.get("location", ""), str(r.status_code))
        r = await client.get("/admin")
        check("C3 GET /admin без логина → redirect /login", r.status_code in (301, 302, 307) and "/login" in r.headers.get("location", ""), str(r.status_code))

        r = await client.get("/api/posts")
        check("C3 GET /api/posts без cookie → 401", r.status_code == 401, str(r.status_code))

        r = await client.get("/rss/news")
        check("C3 GET /rss/news без логина → 200", r.status_code == 200, str(r.status_code))

        r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        check("C2 неверный пароль → 401", r.status_code == 401 and "Неверный логин или пароль" in r.text, str(r.status_code))

        code = None
        for i in range(15):
            code = (await client.post("/api/auth/login", json={"username": "admin", "password": "x"})).status_code
        check("C2 rate limit → 429", code == 429, f"последний={code}")
        auth._attempts.clear()

        r = await client.post("/api/auth/login", json={"username": "admin", "password": "test_password_123"})
        cookie = r.cookies.get("tg_admin")
        check("C1/C4 логин → 200 + httpOnly cookie", r.status_code == 200 and bool(cookie) and "httponly" in r.headers.get("set-cookie", "").lower(), str(r.status_code))

        r = await client.get("/api/admin/channels")
        check("C4 /api/admin/channels → 200", r.status_code == 200 and len(r.json()["channels"]) == 3, str(r.status_code))

        r = await client.get("/admin")
        ok_admin_page = (r.status_code == 200 and "Админка: каналы" in r.text
                         and 'href="/admin" class="on"' in r.text
                         and 'href="/charts"' in r.text and "Каналы" in r.text)
        check("Вкладка /admin: навигация дашборда + каналы и опции", ok_admin_page, str(r.status_code))

        r = await client.post("/api/admin/channels", json={"username": "https://t.me/NewChannel", "feed_type": "news"})
        check("C5/C6 добавление канала → 201", r.status_code == 201 and r.json()["username"] == "newchannel", r.text[:120])
        r = await client.post("/api/admin/channels", json={"username": "@newchannel"})
        check("дубликат канала → 409", r.status_code == 409, str(r.status_code))

        r = await client.get("/rss/news")
        body = r.text
        ok = ("Новость: рост рынка" in body and "Аналитика: цель" not in body
              and "Пост без потока" not in body and "<b>MOEX</b>" not in body
              and "&lt;b&gt;" in body and "<title>Новости</title>" in body)
        check("C10/C11 /rss/news фильтр + escape + title", ok)
        r = await client.get("/rss/analytics")
        check("C10 /rss/analytics только аналитика", "Аналитика: цель" in r.text and "Новость: рост" not in r.text)

        # Персональный фид канала + 404 + OPML
        r = await client.get("/rss/testnews")
        check("/rss/{username}: title = название канала из БД + только его посты",
              r.status_code == 200 and "<title>Тестовые Новости</title>" in r.text
              and "Новость: рост рынка" in r.text and "Аналитика: цель" not in r.text, str(r.status_code))
        r = await client.get("/rss/@testnews")
        check("/rss/@username (с @) тоже работает", r.status_code == 200, str(r.status_code))

        # Кастомное название в RSS: приоритет над TG-названием, сброс пустым
        r = await client.patch(f"/api/admin/channels/{ids['testanalytics']}",
                               json={"rss_title": "Лучшая аналитика рынка"})
        r2 = await client.get("/rss/testanalytics")
        ok_custom = (r.status_code == 200 and "<title>Лучшая аналитика рынка</title>" in r2.text)
        await client.patch(f"/api/admin/channels/{ids['testanalytics']}", json={"rss_title": ""})
        r3 = await client.get("/rss/testanalytics")
        ok_reset = ("<title>Тестовая Аналитика</title>" in r3.text)
        await client.patch(f"/api/admin/channels/{ids['testanalytics']}",
                           json={"rss_title": "Кастом 2"})
        r4 = await client.get("/rss/opml")
        ok_opml = ("Кастом 2" in r4.text)
        check("Название в RSS: кастом → приоритет, пусто → сброс к TG, OPML синхронен",
              ok_custom and ok_reset and ok_opml,
              f"custom={ok_custom} reset={ok_reset} opml={ok_opml}")
        r = await client.get("/rss/no_such_channel")
        check("/rss/{username}: несуществующий канал → 404", r.status_code == 404, str(r.status_code))
        r = await client.get("/rss/opml")
        body_opml = r.text
        check("/rss/opml: все фиды одним файлом",
              r.status_code == 200 and "/rss/news" in body_opml and "/rss/analytics" in body_opml
              and "/rss/testnews" in body_opml and "/rss/testanalytics" in body_opml, str(r.status_code))
        check("/rss/opml: литеральные маршруты не перехвачены {username}",
              "<opml version=\"1.0\">" in body_opml, "")

        await client.put("/api/admin/settings", json={"rss_news_title": "Лента Новостей v2", "rss_analytics_title": "Аналитика v2"})
        r = await client.get("/rss/news")
        check("C11 title фида из админки", "<title>Лента Новостей v2</title>" in r.text)

        r = await client.patch(f"/api/admin/channels/{ids['testnews']}", json={"username": "@renamed_news"})
        r2 = await client.get("/rss/news")
        r3 = await client.get("/rss/renamed_news")
        check("C8 смена username → сводный фид работает", r.status_code == 200 and "Новость: рост рынка" in r2.text, str(r.status_code))
        check("C8 персональный фид после смены username",
              r3.status_code == 200 and "Новость: рост рынка" in r3.text and "Пост без потока" not in r3.text, str(r3.status_code))

        await client.post("/api/auth/change-password", json={"current": "test_password_123", "new": "new_password_456"})
        r = await client.get("/api/admin/channels")
        check("C5 смена пароля: старая cookie → 401", r.status_code == 401, str(r.status_code))
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "new_password_456"})
        check("C5 новый пароль логинит", r.status_code == 200, str(r.status_code))

        await client.post("/api/admin/channels", json={"username": "todelete"})
        chans = (await client.get("/api/admin/channels")).json()
        todelete_id = [c["id"] for c in chans["channels"] if c["username"] == "todelete"][0]
        r = await client.delete(f"/api/admin/channels/{todelete_id}")
        check("C13 жёсткое удаление → 200", r.status_code == 200, str(r.status_code))
        r = await client.patch(f"/api/admin/channels/{ids['testanalytics']}", json={"is_active": False})
        r2 = await client.get("/rss/analytics")
        check("C13 отключение: посты канала исчезли из фида", r.status_code == 200 and "Аналитика: цель" not in r2.text)

        async with async_session() as s:
            s.add(ParseLog(channel_id=ids["testnews"], error_message="[renamed_news] channel not found", finished_at=datetime.now(timezone.utc)))
            await s.commit()
        r = await client.get("/api/admin/channels")
        ch = [c for c in r.json()["channels"] if c["id"] == ids["testnews"]][0]
        check("C7 ошибка парсинга в админке", ch.get("last_error") == "[renamed_news] channel not found", str(ch.get("last_error"))[:50])

        r = await client.get("/rss")
        ok12 = (r.status_code == 200 and "Пост без потока" in r.text and '<rss version="2.0">' in r.text)
        check("C12 старый /rss работает (без фильтра по потокам)", ok12, str(r.status_code))

    total, failed = len(results), sum(1 for _, ok in results if not ok)
    print(f"\n=== {total - failed}/{total} проверок пройдено ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
