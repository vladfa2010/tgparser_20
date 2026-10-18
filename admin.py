#!/usr/bin/env python3
"""
TG Parser v2 — Admin panel.

Channel management lives in the DB now (not env): the parser picks up
changes on its next cron run. Feeds are grouped via channels.feed_type
('news' | 'analytics').
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from auth import require_admin
from db import Channel, ParseLog, Post, async_session, get_setting, set_setting

router = APIRouter(dependencies=[Depends(require_admin)])

VALID_FEED_TYPES = (None, "", "news", "analytics")


def normalize_username(raw: str) -> str:
    """@name / https://t.me/name / t.me/name → name"""
    u = raw.strip()
    u = re.sub(r"^https?://(t\.me|telegram\.me)/", "", u)
    u = u.lstrip("@").strip().strip("/")
    return u.lower()


# ─── Schemas ─────────────────────────────────────────────────
class ChannelCreate(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    feed_type: str | None = None


class ChannelPatch(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=255)
    feed_type: str | None = None
    is_active: bool | None = None


class FeedTitles(BaseModel):
    rss_news_title: str | None = Field(default=None, max_length=300)
    rss_analytics_title: str | None = Field(default=None, max_length=300)


# ─── API ─────────────────────────────────────────────────────
@router.get("/api/admin/channels")
async def list_channels():
    async with async_session() as session:
        rows = (await session.execute(
            text("""
                SELECT c.id, c.username, c.title, c.feed_type, c.is_active,
                       c.backfill_full, c.subscriber_count, c.last_parsed_at,
                       (SELECT count(*) FROM posts p WHERE p.channel_id = c.id) AS post_count,
                       (SELECT max(p.published_at) FROM posts p WHERE p.channel_id = c.id) AS last_post_at,
                       (SELECT pl.error_message FROM parse_logs pl
                         WHERE pl.channel_id = c.id AND pl.error_message IS NOT NULL
                         ORDER BY pl.finished_at DESC NULLS LAST LIMIT 1) AS last_error,
                       (SELECT max(pl.finished_at) FROM parse_logs pl
                         WHERE pl.channel_id = c.id) AS last_run_at
                FROM channels c
                ORDER BY c.is_active DESC, c.username
            """)
        )).mappings().all()
        return {"channels": [dict(r) for r in rows]}


@router.post("/api/admin/channels", status_code=201)
async def create_channel(body: ChannelCreate):
    username = normalize_username(body.username)
    if not username:
        raise HTTPException(400, "Пустой username")
    feed = body.feed_type if body.feed_type in ("news", "analytics") else None
    async with async_session() as session:
        exists = (await session.execute(
            select(Channel).where(Channel.username == username))).scalar_one_or_none()
        if exists:
            raise HTTPException(409, f"Канал @{username} уже есть в списке")
        session.add(Channel(username=username, is_active=True, feed_type=feed,
                            title="@" + username))
        await session.commit()
    return {"ok": True, "username": username,
            "note": "Канал подхватится парсером на ближайшем прогоне крона (первичная докачка — 2000 последних постов)"}


@router.patch("/api/admin/channels/{channel_id}")
async def patch_channel(channel_id: int, body: ChannelPatch):
    async with async_session() as session:
        channel = (await session.execute(
            select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
        if not channel:
            raise HTTPException(404, "Канал не найден")
        if body.username is not None:
            username = normalize_username(body.username)
            if not username:
                raise HTTPException(400, "Пустой username")
            clash = (await session.execute(
                select(Channel).where(Channel.username == username, Channel.id != channel_id)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(409, f"Имя @{username} занято другим каналом")
            channel.username = username
        if body.feed_type is not None:
            if body.feed_type not in VALID_FEED_TYPES:
                raise HTTPException(400, "feed_type: news, analytics или пусто")
            channel.feed_type = body.feed_type or None
        if body.is_active is not None:
            channel.is_active = body.is_active
        await session.commit()
    return {"ok": True}


@router.delete("/api/admin/channels/{channel_id}")
async def delete_channel(channel_id: int):
    async with async_session() as session:
        channel = (await session.execute(
            select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
        if not channel:
            raise HTTPException(404, "Канал не найден")
        await session.delete(channel)
        await session.commit()
    return {"ok": True}


@router.post("/api/admin/channels/{channel_id}/backfill")
async def request_backfill(channel_id: int):
    async with async_session() as session:
        channel = (await session.execute(
            select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
        if not channel:
            raise HTTPException(404, "Канал не найден")
        channel.backfill_full = True
        await session.commit()
    return {"ok": True, "note": "Полная докачка запустится на ближайшем прогоне крона"}


@router.get("/api/admin/settings")
async def get_settings():
    async with async_session() as session:
        return {
            "rss_news_title": await get_setting(session, "rss_news_title", ""),
            "rss_analytics_title": await get_setting(session, "rss_analytics_title", ""),
        }


@router.put("/api/admin/settings")
async def put_settings(body: FeedTitles):
    async with async_session() as session:
        if body.rss_news_title is not None:
            await set_setting(session, "rss_news_title", body.rss_news_title)
        if body.rss_analytics_title is not None:
            await set_setting(session, "rss_analytics_title", body.rss_analytics_title)
        await session.commit()
    return {"ok": True}


# ─── Page ────────────────────────────────────────────────────
ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Админка — TG Parser</title>
<style>
body{margin:0;background:#0f172a;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;padding:24px}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:16px;margin:28px 0 10px;color:#94a3b8}
a{color:#60a5fa;text-decoration:none;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid #1e293b;text-align:left;vertical-align:middle}
th{color:#64748b;font-weight:600}
select,input{background:#1e293b;border:1px solid #475569;border-radius:6px;color:#e2e8f0;padding:6px 8px;font-size:13px}
button{background:#2563eb;border:none;border-radius:6px;color:#fff;padding:6px 12px;font-size:13px;cursor:pointer}
button:hover{background:#1d4ed8}
button.danger{background:#b91c1c}button.danger:hover{background:#991b1b}
button.ghost{background:#334155}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.err{color:#f87171}.ok{color:#4ade80}.muted{color:#64748b}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px}
.badge.news{background:#1e3a8a}.badge.analytics{background:#5b21b6}
.off{opacity:.5}
.msg{min-height:18px;font-size:13px;margin:6px 0}
</style></head><body>
<h1>TG Parser 2.0 — админка <span class="muted">·</span> <a href="/">← Дашборд</a>
<span class="muted">·</span> <a href="#" id="logout">Выйти</a></h1>

<h2>Каналы</h2>
<div class="row">
<input id="new-username" placeholder="@channel или ссылка t.me/..." size="30">
<select id="new-feed"><option value="">— без потока</option><option value="news">Новости</option><option value="analytics">Аналитика</option></select>
<button id="add">Добавить</button>
<span class="muted">первичная докачка — 2000 последних постов</span>
</div>
<div class="msg" id="msg1"></div>
<table><thead><tr>
<th>@username</th><th>Название</th><th>Постов</th><th>Последний пост</th><th>Прогон</th><th>Поток</th><th>Активен</th><th>Действия</th>
</tr></thead><tbody id="rows"></tbody></table>

<h2>Названия RSS-потоков</h2>
<div class="row">
<input id="t-news" placeholder="Новости" size="30">
<input id="t-analytics" placeholder="Аналитика" size="30">
<button id="save-titles">Сохранить</button>
</div>
<div class="msg" id="msg2"></div>
<p class="muted" id="feed-urls"></p>

<h2>Смена пароля</h2>
<div class="row">
<input id="cur-pwd" type="password" placeholder="Текущий пароль">
<input id="new-pwd" type="password" placeholder="Новый (мин. 8 символов)">
<button id="save-pwd">Сменить</button>
</div>
<div class="msg" id="msg3"></div>

<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function api(method, url, body){
  var r = await fetch(url, {method:method, headers:{'Content-Type':'application/json'},
    body: body?JSON.stringify(body):undefined});
  var d = await r.json().catch(function(){return {};});
  if (!r.ok) throw new Error(d.detail || d.error || ('Ошибка '+r.status));
  return d;
}
async function load(){
  var d = await api('GET','/api/admin/channels');
  document.getElementById('rows').innerHTML = d.channels.map(function(c){
    var feedSel = '<select onchange="patch('+c.id+',{feed_type:this.value})">'
      + '<option value=""'+(c.feed_type?'':' selected')+'>—</option>'
      + '<option value="news"'+(c.feed_type==='news'?' selected':'')+'>Новости</option>'
      + '<option value="analytics"'+(c.feed_type==='analytics'?' selected':'')+'>Аналитика</option></select>';
    var err = c.last_error ? '<div class="err" title="'+esc(c.last_error)+'">'+esc(c.last_error.slice(0,60))+'</div>' : '';
    return '<tr class="'+(c.is_active?'':'off')+'">'
      + '<td>@{0}</td>'.replace('{0}', esc(c.username))
      + '<td>'+esc(c.title||'')+'</td>'
      + '<td>'+(c.post_count||0)+'</td>'
      + '<td>'+(c.last_post_at?esc(c.last_post_at.slice(0,16).replace('T',' ')):'—')+'</td>'
      + '<td>'+(c.last_run_at?esc(c.last_run_at.slice(0,16).replace('T',' ')):'—')+err+'</td>'
      + '<td>'+feedSel+'</td>'
      + '<td><button class="ghost" onclick="patch('+c.id+',{is_active:'+(!c.is_active)+'})">'+(c.is_active?'выкл':'вкл')+'</button></td>'
      + '<td><button class="ghost" onclick="backfill('+c.id+')">Докачать всё</button> '
      + '<button class="danger" onclick="del('+c.id+",'"+esc(c.username)+"')\">Удалить</button></td></tr>";
  }).join('');
  var s = await api('GET','/api/admin/settings');
  document.getElementById('t-news').value = s.rss_news_title || '';
  document.getElementById('t-analytics').value = s.rss_analytics_title || '';
  document.getElementById('feed-urls').textContent =
    'Фиды: /rss/news ('+(s.rss_news_title||'Новости')+') и /rss/analytics ('+(s.rss_analytics_title||'Аналитика')+')';
}
async function patch(id, body){ try{ await api('PATCH','/api/admin/channels/'+id, body); load(); }catch(e){ alert(e.message); } }
async function backfill(id){ try{ var d = await api('POST','/api/admin/channels/'+id+'/backfill'); alert(d.note); }catch(e){ alert(e.message); } }
async function del(id, name){ if(!confirm('Удалить @'+name+'? Посты выпадут из RSS и статистики.')) return;
  try{ await api('DELETE','/api/admin/channels/'+id); load(); }catch(e){ alert(e.message); } }
document.getElementById('add').onclick = async function(){
  try{ var d = await api('POST','/api/admin/channels',
    {username:document.getElementById('new-username').value,
     feed_type:document.getElementById('new-feed').value || null});
    document.getElementById('msg1').textContent = d.note; document.getElementById('msg1').className='msg ok';
    document.getElementById('new-username').value=''; load();
  }catch(e){ document.getElementById('msg1').textContent = e.message; document.getElementById('msg1').className='msg err'; }
};
document.getElementById('save-titles').onclick = async function(){
  try{ await api('PUT','/api/admin/settings',
    {rss_news_title:document.getElementById('t-news').value,
     rss_analytics_title:document.getElementById('t-analytics').value});
    document.getElementById('msg2').textContent='Сохранено'; document.getElementById('msg2').className='msg ok'; load();
  }catch(e){ document.getElementById('msg2').textContent=e.message; document.getElementById('msg2').className='msg err'; }
};
document.getElementById('save-pwd').onclick = async function(){
  try{ await api('POST','/api/auth/change-password',
    {current:document.getElementById('cur-pwd').value, new:document.getElementById('new-pwd').value});
    document.getElementById('msg3').textContent='Пароль сменён, сессии обновлены';
    document.getElementById('msg3').className='msg ok';
    document.getElementById('cur-pwd').value=''; document.getElementById('new-pwd').value='';
  }catch(e){ document.getElementById('msg3').textContent=e.message; document.getElementById('msg3').className='msg err'; }
};
document.getElementById('logout').onclick = async function(e){
  e.preventDefault(); await api('POST','/api/auth/logout'); location.href='/login';
};
load();
</script>
</body></html>"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(content=ADMIN_HTML)
