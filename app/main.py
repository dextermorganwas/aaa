import hashlib
import hmac
import os
import secrets
import time
from email.utils import formatdate
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Database
from .resolver import Resolver
from .util import parse_art_request, safe_key
from .models import Lookup

settings = get_settings()
db = Database(settings.db_path)
resolver = Resolver(settings, db)

SESSION_COOKIE = 'art_proxy_admin_session'

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await resolver.close()
    await db.close()

app = FastAPI(title='Stremio Art Proxy', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='/app/app/static'), name='static')


def _session_signature(payload: str) -> str:
    return hmac.new(settings.admin_token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    expires = int(time.time()) + max(1, settings.admin_session_hours) * 3600
    nonce = secrets.token_urlsafe(16)
    payload = f'{expires}.{nonce}'
    return f'{payload}.{_session_signature(payload)}'


def _valid_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        expires_s, nonce, signature = value.split('.', 2)
        expires = int(expires_s)
    except (ValueError, TypeError):
        return False
    if expires <= int(time.time()) or not settings.admin_token:
        return False
    payload = f'{expires}.{nonce}'
    expected = _session_signature(payload)
    return hmac.compare_digest(signature, expected)


def _token_valid(request: Request) -> bool:
    token = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not token:
        token = request.query_params.get('token', '')
    return bool(settings.admin_token) and hmac.compare_digest(token, settings.admin_token)


def auth(request: Request):
    if not settings.admin_enabled:
        return
    if _valid_session(request.cookies.get(SESSION_COOKIE)) or _token_valid(request):
        return
    raise HTTPException(401, 'Unauthorized')


@app.get('/health')
async def health():
    return {'ok': True}


@app.get('/', response_class=HTMLResponse)
async def home():
    return RedirectResponse('/admin')




@app.get('/admin', response_class=HTMLResponse)
async def admin(request: Request):
    if settings.admin_enabled and not (_valid_session(request.cookies.get(SESSION_COOKIE)) or _token_valid(request)):
        return RedirectResponse('/admin/login')
    with open('/app/app/static/admin.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.get('/admin/login', response_class=HTMLResponse)
async def admin_login(request: Request):
    if not settings.admin_enabled:
        return RedirectResponse('/admin')
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse('/admin')
    with open('/app/app/static/login.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.post('/api/admin/login')
async def admin_login_post(request: Request):
    if not settings.admin_enabled:
        return {'ok': True}
    form = await request.form()
    token = str(form.get('token', ''))
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(401, 'Invalid admin token')
    response = RedirectResponse('/admin', status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _make_session(),
        max_age=max(1, settings.admin_session_hours) * 3600,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite='strict',
        path='/',
    )
    return response


@app.post('/api/admin/logout')
async def admin_logout(request: Request):
    auth(request)
    response = RedirectResponse('/admin/login', status_code=303)
    response.delete_cookie(SESSION_COOKIE, path='/')
    return response


@app.get('/api/admin/session')
async def admin_session(request: Request):
    auth(request)
    return {'authenticated': True}


@app.get('/api/admin/items')
async def admin_items(request: Request, q: str = ''):
    auth(request)
    rows = await db.list_items(q)
    return {'items': [dict(r) for r in rows]}


@app.get('/api/admin/items/{item_id}')
async def admin_item(request: Request, item_id: int):
    auth(request)
    item = await db.get_item(item_id)
    if not item:
        raise HTTPException(404)
    out = {'item': dict(item), 'art': {}}
    for art in ('poster', 'backdrop', 'logo'):
        sel = await db.get_selection(item_id, art)
        cands = await db.list_candidates(item_id, art)
        out['art'][art] = {
            'selection': dict(sel) if sel else None,
            'candidates': [dict(c) for c in cands],
        }
    return out


@app.get('/api/admin/items/{item_id}/image/{art_type}')
async def admin_image(request: Request, item_id: int, art_type: str):
    auth(request)
    if art_type not in ('poster', 'backdrop', 'logo'):
        raise HTTPException(400)
    sel = await db.get_selection(item_id, art_type)
    if not sel or not sel['local_path'] or not os.path.exists(sel['local_path']):
        raise HTTPException(404)
    return FileResponse(
        sel['local_path'],
        media_type=sel['content_type'] or 'image/jpeg',
        headers={'Cache-Control': 'private, max-age=60'},
    )


@app.post('/api/admin/items/{item_id}/refresh/{art_type}')
async def admin_refresh(request: Request, item_id: int, art_type: str):
    auth(request)
    if art_type not in ('poster', 'backdrop', 'logo'):
        raise HTTPException(400)
    return {'candidates': [c.__dict__ for c in await resolver.refresh_candidates(item_id, art_type)]}


@app.post('/api/admin/items/{item_id}/override/{art_type}')
async def admin_override(request: Request, item_id: int, art_type: str):
    auth(request)
    body = await request.json()
    url = body.get('url')
    provider = body.get('provider', 'manual')
    if not url:
        raise HTTPException(400, 'url required')
    try:
        sel = await resolver.override(item_id, art_type, url, provider)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return dict(sel)


@app.get('/{art_type}/{spec}')
async def artwork(art_type: str, spec: str, request: Request):
    try:
        kind, ids = parse_art_request(art_type, spec)
        lookup = Lookup(kind, ids.get('tmdb_id'), ids.get('imdb_id'), ids.get('tvdb_id'))
        await resolver.resolve(lookup, art_type)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc))
    item = await db.upsert_item(lookup)
    sel = await db.get_selection(item['id'], art_type)
    if not sel or not sel['local_path'] or not os.path.exists(sel['local_path']):
        raise HTTPException(404, 'image unavailable')
    headers = {
        'Cache-Control': 'public, max-age=604800, stale-while-revalidate=86400',
        'ETag': '"' + safe_key(sel['provider'], sel['source_url'], os.path.getsize(sel['local_path'])) + '"',
        'X-Art-Provider': sel['provider'],
        'X-Art-Source': sel['source_url'] or '',
        'Last-Modified': formatdate(os.path.getmtime(sel['local_path']), usegmt=True),
        'Vary': 'Accept',
    }
    if request.headers.get('if-none-match') == headers['ETag']:
        return Response(status_code=304, headers=headers)
    return FileResponse(sel['local_path'], media_type=sel['content_type'] or 'image/jpeg', headers=headers)

