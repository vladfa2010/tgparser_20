#!/usr/bin/env python3
"""
TG Parser v2 — Admin panel.

Channel management lives in the DB (not env): the parser picks up
changes on its next cron run. Channels are grouped via feed_groups —
each group is its own RSS feed /rss/{slug}.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text

import rsscache
from auth import require_admin
from db import Channel, FeedGroup, ParseLog, Post, async_session

router = APIRouter(dependencies=[Depends(require_admin)])

VALID_FEED_TYPES = (None, "", "news", "analytics")

SLUG_RE = re.compile(r"[^a-z0-9а-яё\-]+")
SLUG_DASHES = re.compile(r"-+")


def normalize_username(raw: str) -> str:
    """@name / https://t.me/name / t.me/name → name"""
    u = raw.strip()
    u = re.sub(r"^https?://(t\.me|telegram\.me)/", "", u)
    u = u.lstrip("@").strip().strip("/")
    return u.lower()


def normalize_slug(raw: str) -> str:
    """«Мои новости» → «мои-новости». Пустой результат → ''."""
    s = SLUG_RE.sub("-", raw.strip().lower().replace(" ", "-"))
    return SLUG_DASHES.sub("-", s).strip("-")


# ─── Schemas ─────────────────────────────────────────────────
class ChannelCreate(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    feed_type: str | None = None  # legacy, игнорируется — канал добавляется без группы


class ChannelPatch(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=255)
    group_id: int | None = None   # null → не менять; 0 → убрать из группы
    is_active: bool | None = None
    rss_title: str | None = Field(default=None, max_length=500)  # '' → сброс к названию из TG


class GroupCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    slug: str | None = Field(default=None, max_length=200)


class GroupPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    slug: str | None = Field(default=None, max_length=200)


# ─── Channels API ────────────────────────────────────────────
@router.get("/api/admin/channels")
async def list_channels():
    async with async_session() as session:
        rows = (await session.execute(
            text("""
                SELECT c.id, c.username, c.title, c.group_id, c.is_active,
                       c.backfill_full, c.rss_title, c.subscriber_count, c.last_parsed_at,
                       (SELECT count(*) FROM posts p WHERE p.channel_id = c.id) AS post_count,
                       (SELECT max(p.published_at) FROM posts p WHERE p.channel_id = c.id) AS last_post_at,
                       (SELECT g.title FROM feed_groups g WHERE g.id = c.group_id) AS group_title,
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
    async with async_session() as session:
        exists = (await session.execute(
            select(Channel).where(Channel.username == username))).scalar_one_or_none()
        if exists:
            raise HTTPException(409, f"Канал @{username} уже есть в списке")
        session.add(Channel(username=username, is_active=True, title="@" + username))
        await session.commit()
    rsscache.invalidate()
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
        if body.group_id is not None:
            if body.group_id == 0:
                channel.group_id = None
            else:
                group = await session.get(FeedGroup, body.group_id)
                if not group:
                    raise HTTPException(404, "Группа не найдена")
                channel.group_id = group.id
        if body.is_active is not None:
            channel.is_active = body.is_active
        if body.rss_title is not None:
            channel.rss_title = body.rss_title.strip() or None
        await session.commit()
    rsscache.invalidate()
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
    rsscache.invalidate()
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
    rsscache.invalidate()
    return {"ok": True, "note": "Полная докачка запустится на ближайшем прогоне крона"}


# ─── Groups API (CRUD) ───────────────────────────────────────
@router.get("/api/admin/groups")
async def list_groups():
    async with async_session() as session:
        rows = (await session.execute(
            text("""
                SELECT g.id, g.slug, g.title,
                       (SELECT count(*) FROM channels c WHERE c.group_id = g.id) AS channel_count
                FROM feed_groups g
                ORDER BY g.title
            """)
        )).mappings().all()
        return {"groups": [dict(r) for r in rows]}


@router.post("/api/admin/groups", status_code=201)
async def create_group(body: GroupCreate):
    title = body.title.strip()
    slug = normalize_slug(body.slug or title)
    if not slug:
        raise HTTPException(400, "Не получилось сделать slug из названия — задайте slug явно (латиница/цифры/дефис)")
    async with async_session() as session:
        clash = (await session.execute(
            select(FeedGroup).where(FeedGroup.slug == slug))).scalar_one_or_none()
        if clash:
            raise HTTPException(409, f"Группа со slug «{slug}» уже существует")
        group = FeedGroup(slug=slug, title=title)
        session.add(group)
        await session.commit()
    rsscache.invalidate()
    return {"ok": True, "slug": slug, "feed": f"/rss/{slug}"}


@router.patch("/api/admin/groups/{group_id}")
async def patch_group(group_id: int, body: GroupPatch):
    async with async_session() as session:
        group = await session.get(FeedGroup, group_id)
        if not group:
            raise HTTPException(404, "Группа не найдена")
        if body.slug is not None:
            slug = normalize_slug(body.slug)
            if not slug:
                raise HTTPException(400, "Пустой slug")
            clash = (await session.execute(
                select(FeedGroup).where(FeedGroup.slug == slug, FeedGroup.id != group_id)
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(409, f"Slug «{slug}» занят другой группой")
            group.slug = slug
        if body.title is not None:
            group.title = body.title.strip()
        await session.commit()
    rsscache.invalidate()
    return {"ok": True}


@router.delete("/api/admin/groups/{group_id}")
async def delete_group(group_id: int):
    async with async_session() as session:
        group = await session.get(FeedGroup, group_id)
        if not group:
            raise HTTPException(404, "Группа не найдена")
        # каналы группы возвращаем в «без группы» (персональные фиды не страдают)
        await session.execute(
            text("UPDATE channels SET group_id = NULL WHERE group_id = :gid"), {"gid": group_id})
        await session.delete(group)
        await session.commit()
    rsscache.invalidate()
    return {"ok": True}


# ─── Page ────────────────────────────────────────────────────
ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — TG Parser Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a1a;color:#e2e8f0;line-height:1.5}
.wrap{max-width:1200px;margin:0 auto;padding:24px}
header{margin-bottom:24px}h1{color:#00d4aa;font-size:28px;font-weight:700}
.sub{color:#64748b;font-size:14px;margin-top:4px}
nav{display:flex;gap:4px;background:#0f172a;padding:4px;border-radius:10px;border:1px solid #1e293b;width:fit-content;margin-bottom:24px;flex-wrap:wrap}
nav a{color:#64748b;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:500;display:flex;align-items:center;gap:6px}
nav a:hover{color:#e2e8f0;background:#1e293b}
nav a.on{color:#0a0a1a;background:#00d4aa;font-weight:600}
h2{color:#e2e8f0;font-size:18px;margin:28px 0 12px}
.muted{color:#64748b;font-size:13px}
.msg{min-height:18px;font-size:13px;margin:8px 0}
.err{color:#f87171}.ok{color:#00d4aa}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.row input,.row select{background:#0f172a;border:1px solid #1e293b;color:#e2e8f0;padding:10px 14px;border-radius:8px;font-size:14px;outline:none}
.row input:focus,.row select:focus{border-color:#00d4aa}
.btn{background:#00d4aa;color:#0a0a1a;border:none;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{opacity:.85}
.btn.sm{padding:6px 12px;font-size:13px}
.btn.ghost{background:#1e293b;color:#e2e8f0}
.btn.danger{background:#7f1d1d;color:#f87171}
table{width:100%;border-collapse:separate;border-spacing:0;background:#0f172a;border:1px solid #1e293b;border-radius:12px;overflow:hidden;font-size:13px}
th{background:#131a2e;color:#64748b;font-weight:600;text-align:left;padding:10px 12px}
td{padding:10px 12px;border-top:1px solid #1e293b;vertical-align:middle}
tr.off{opacity:.45}
td select{background:#0a0a1a;border:1px solid #1e293b;color:#e2e8f0;padding:5px 8px;border-radius:6px;font-size:13px}
td input{background:#0a0a1a;border:1px solid #1e293b;color:#e2e8f0;padding:5px 8px;border-radius:6px;font-size:13px}
.chip{display:inline-block;background:#1e293b;color:#00d4aa;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:500}
a.feedlink{color:#60a5fa;text-decoration:none;font-size:12px}
a.feedlink:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
<header><h1>TG Parser Dashboard</h1><p class="sub">Админка: каналы, группы фидов, доступ</p></header>
<nav>
<a href="/">&#128240; Posts</a>
<a href="/charts">&#128202; Charts</a>
<a href="/analytics">&#128270; Analytics</a>
<a href="/tag-daily">&#128200; Stock</a>
<a href="/sentiment">&#129504; Sentiment</a>
<a href="/viral">&#128293; Viral</a>
<a href="/sectors">&#127775; Sectors</a>
<a href="/wordcloud">&#9729;&#65039; Words</a>
<a href="/crossmarket">&#127758; Macro</a>
<a href="/admin" class="on">&#9881;&#65039; Admin</a>
</nav>

<h2>Группы фидов <span class="muted">— каждая группа = свой RSS-поток /rss/{slug}</span></h2>
<div class="row">
<input id="new-group-title" placeholder="Название группы (напр. «Дивиденды»)" size="34">
<input id="new-group-slug" placeholder="slug (необязательно)" size="20">
<button class="btn" id="add-group">Создать группу</button>
</div>
<div class="msg" id="msg0"></div>
<table><thead><tr>
<th>Название</th><th>URL фида</th><th>Каналов</th><th>Действия</th>
</tr></thead><tbody id="group-rows"></tbody></table>

<h2>Каналы</h2>
<div class="row">
<input id="new-username" placeholder="@channel или ссылка t.me/..." size="32">
<button class="btn" id="add">Добавить канал</button>
<span class="muted">первичная докачка — 2000 последних постов; группу назначьте ниже в таблице</span>
</div>
<div class="msg" id="msg1"></div>
<table><thead><tr>
<th>@username</th><th>Название</th><th>Название в RSS</th><th>Группа</th><th>Постов</th><th>Последний пост</th><th>Прогон / ошибка</th><th>Активен</th><th>Фид</th><th>Действия</th>
</tr></thead><tbody id="rows"></tbody></table>

<h2>Доступ</h2>
<div class="row">
<input id="cur-pwd" type="password" placeholder="Текущий пароль">
<input id="new-pwd" type="password" placeholder="Новый (мин. 8 символов)">
<button class="btn" id="save-pwd">Сменить пароль</button>
<button class="btn ghost" id="logout">Выйти</button>
</div>
<div class="msg" id="msg3"></div>
</div>

<script>
var GROUPS = [];
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function api(method, url, body){
  var r = await fetch(url, {method:method, headers:{'Content-Type':'application/json'},
    body: body?JSON.stringify(body):undefined});
  var d = await r.json().catch(function(){return {};});
  if (!r.ok) throw new Error(d.detail || d.error || ('Ошибка '+r.status));
  return d;
}
function groupSelect(c){
  var s = '<select onchange="patch('+c.id+',{group_id:this.value===""?0:parseInt(this.value)})">'
    + '<option value=""'+((c.group_id==null)?' selected':'')+'>— без группы</option>';
  GROUPS.forEach(function(g){
    s += '<option value="'+g.id+'"'+(c.group_id===g.id?' selected':'')+'>'+esc(g.title)+'</option>';
  });
  return s + '</select>';
}
async function load(){
  var gd = await api('GET','/api/admin/groups');
  GROUPS = gd.groups;
  document.getElementById('group-rows').innerHTML = GROUPS.map(function(g){
    return '<tr><td><span class="chip">'+esc(g.title)+'</span></td>'
      + '<td><a class="feedlink" href="/rss/'+esc(g.slug)+'" target="_blank">/rss/'+esc(g.slug)+'</a></td>'
      + '<td>'+(g.channel_count||0)+'</td>'
      + '<td><button class="btn sm ghost" onclick="renameGroup('+g.id+",'"+esc(g.title)+"')\">Переименовать</button> "
      + '<button class="btn sm danger" onclick="delGroup('+g.id+",'"+esc(g.title)+"')\">Удалить</button></td></tr>";
  }).join('') || '<tr><td colspan="4" class="muted">Групп нет — создайте первую выше</td></tr>';

  var d = await api('GET','/api/admin/channels');
  document.getElementById('rows').innerHTML = d.channels.map(function(c){
    var rssInput = '<input size="18" placeholder="как в TG" value="'+esc(c.rss_title||'')+'" '
      + 'onchange="patch('+c.id+',{rss_title:this.value})">';
    var err = c.last_error ? '<div class="err" title="'+esc(c.last_error)+'">&#9888;&#65039; '+esc(c.last_error.slice(0,50))+'</div>' : '';
    var run = c.last_run_at ? esc(c.last_run_at.slice(0,16).replace('T',' ')) : '—';
    var last = c.last_post_at ? esc(c.last_post_at.slice(0,16).replace('T',' ')) : '—';
    return '<tr class="'+(c.is_active?'':'off')+'">'
      + '<td>@'+esc(c.username)+'</td>'
      + '<td>'+esc(c.title||'')+'</td>'
      + '<td>'+rssInput+'</td>'
      + '<td>'+groupSelect(c)+'</td>'
      + '<td>'+(c.post_count||0)+'</td>'
      + '<td>'+last+'</td>'
      + '<td>'+run+err+'</td>'
      + '<td><button class="btn sm ghost" onclick="patch('+c.id+',{is_active:'+(!c.is_active)+'})">'+(c.is_active?'выкл':'вкл')+'</button></td>'
      + '<td><a class="feedlink" href="/rss/'+esc(c.username)+'" target="_blank">RSS</a></td>'
      + '<td><button class="btn sm ghost" onclick="backfill('+c.id+')">Докачать всё</button> '
      + '<button class="btn sm danger" onclick="del('+c.id+",'"+esc(c.username)+"')\">Удалить</button></td></tr>";
  }).join('');
}
async function patch(id, body){ try{ await api('PATCH','/api/admin/channels/'+id, body); load(); }catch(e){ alert(e.message); } }
async function backfill(id){ try{ var d = await api('POST','/api/admin/channels/'+id+'/backfill'); alert(d.note); }catch(e){ alert(e.message); } }
async function del(id, name){ if(!confirm('Удалить @'+name+'? Посты выпадут из RSS и статистики.')) return;
  try{ await api('DELETE','/api/admin/channels/'+id); load(); }catch(e){ alert(e.message); } }
async function renameGroup(id, title){
  var t = prompt('Новое название группы:', title);
  if (t === null || !t.trim()) return;
  try{ await api('PATCH','/api/admin/groups/'+id, {title:t.trim()}); load(); }catch(e){ alert(e.message); }
}
async function delGroup(id, title){
  if(!confirm('Удалить группу «'+title+'»? Каналы останутся (без группы), их фиды не пострадают.')) return;
  try{ await api('DELETE','/api/admin/groups/'+id); load(); }catch(e){ alert(e.message); }
}
document.getElementById('add-group').onclick = async function(){
  try{ var d = await api('POST','/api/admin/groups',
    {title:document.getElementById('new-group-title').value,
     slug:document.getElementById('new-group-slug').value || null});
    var m=document.getElementById('msg0'); m.textContent='Создана: '+d.feed; m.className='msg ok';
    document.getElementById('new-group-title').value=''; document.getElementById('new-group-slug').value=''; load();
  }catch(e){ var m=document.getElementById('msg0'); m.textContent=e.message; m.className='msg err'; }
};
document.getElementById('add').onclick = async function(){
  try{ var d = await api('POST','/api/admin/channels',
    {username:document.getElementById('new-username').value});
    var m=document.getElementById('msg1'); m.textContent = d.note; m.className='msg ok';
    document.getElementById('new-username').value=''; load();
  }catch(e){ var m=document.getElementById('msg1'); m.textContent = e.message; m.className='msg err'; }
};
document.getElementById('save-pwd').onclick = async function(){
  try{ await api('POST','/api/auth/change-password',
    {current:document.getElementById('cur-pwd').value, new:document.getElementById('new-pwd').value});
    var m=document.getElementById('msg3'); m.textContent='Пароль сменён, все сессии завершены'; m.className='msg ok';
    document.getElementById('cur-pwd').value=''; document.getElementById('new-pwd').value='';
  }catch(e){ var m=document.getElementById('msg3'); m.textContent=e.message; m.className='msg err'; }
};
document.getElementById('logout').onclick = async function(){ await api('POST','/api/auth/logout'); location.href='/login'; };
load();
</script>
</body>
</html>"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(content=ADMIN_HTML)
