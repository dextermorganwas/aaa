import os
import json
import secrets
from email.utils import formatdate
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings
from .db import Database
from .resolver import Resolver
from .util import parse_art_request, safe_key
from .models import Lookup

settings=get_settings(); db=Database(settings.db_path); resolver=Resolver(settings,db)

@asynccontextmanager
async def lifespan(app:FastAPI):
    await db.connect()
    yield
    await resolver.close(); await db.close()

app=FastAPI(title='Stremio Art Proxy',lifespan=lifespan)
app.mount('/static',StaticFiles(directory='/app/app/static'),name='static')

def auth(request:Request):
    if not settings.admin_enabled:return
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip() or request.query_params.get('token','')
    if not secrets.compare_digest(token,settings.admin_token): raise HTTPException(401,'Unauthorized')

@app.get('/health')
async def health():return {'ok':True}

@app.get('/',response_class=HTMLResponse)
async def home():
    return RedirectResponse('/admin')

@app.get('/{art_type}/{spec}')
async def artwork(art_type:str,spec:str,request:Request):
    try:
        kind,ids=parse_art_request(art_type,spec)
        lookup=Lookup(kind,ids.get('tmdb_id'),ids.get('imdb_id'),ids.get('tvdb_id'))
        res=await resolver.resolve(lookup,art_type)
    except (ValueError,LookupError) as exc:
        raise HTTPException(404,str(exc))
    item=await db.upsert_item(lookup)
    sel=await db.get_selection(item['id'],art_type)
    if not sel or not sel['local_path'] or not os.path.exists(sel['local_path']): raise HTTPException(404,'image unavailable')
    headers={
        'Cache-Control':'public, max-age=604800, stale-while-revalidate=86400',
        'ETag':'"'+safe_key(sel['provider'],sel['source_url'],os.path.getsize(sel['local_path']))+'"',
        'X-Art-Provider':sel['provider'],
        'X-Art-Source':sel['source_url'] or '',
        'Last-Modified':formatdate(os.path.getmtime(sel['local_path']), usegmt=True),
        'Vary':'Accept',
    }
    if request.headers.get('if-none-match')==headers['ETag']:
        return Response(status_code=304,headers=headers)
    return FileResponse(sel['local_path'],media_type=sel['content_type'] or 'image/jpeg',headers=headers)

@app.get('/admin',response_class=HTMLResponse)
async def admin(request:Request):
    auth(request)
    with open('/app/app/static/admin.html','r',encoding='utf-8') as f:return f.read()

@app.get('/api/admin/items')
async def admin_items(request:Request,q:str=''):
    auth(request); rows=await db.list_items(q)
    return {'items':[dict(r) for r in rows]}

@app.get('/api/admin/items/{item_id}')
async def admin_item(request:Request,item_id:int):
    auth(request); item=await db.get_item(item_id)
    if not item: raise HTTPException(404)
    out={'item':dict(item),'art':{}}
    for art in ('poster','backdrop','logo'):
        sel=await db.get_selection(item_id,art); cands=await db.list_candidates(item_id,art)
        out['art'][art]={'selection':dict(sel) if sel else None,'candidates':[dict(c) for c in cands]}
    return out


@app.get('/api/admin/items/{item_id}/image/{art_type}')
async def admin_image(request:Request,item_id:int,art_type:str):
    auth(request)
    if art_type not in ('poster','backdrop','logo'): raise HTTPException(400)
    sel=await db.get_selection(item_id,art_type)
    if not sel or not sel['local_path'] or not os.path.exists(sel['local_path']): raise HTTPException(404)
    return FileResponse(sel['local_path'],media_type=sel['content_type'] or 'image/jpeg',headers={'Cache-Control':'private, max-age=60'})

@app.post('/api/admin/items/{item_id}/refresh/{art_type}')
async def admin_refresh(request:Request,item_id:int,art_type:str):
    auth(request)
    if art_type not in ('poster','backdrop','logo'):raise HTTPException(400)
    return {'candidates':[c.__dict__ for c in await resolver.refresh_candidates(item_id,art_type)]}

@app.post('/api/admin/items/{item_id}/override/{art_type}')
async def admin_override(request:Request,item_id:int,art_type:str):
    auth(request)
    body=await request.json(); url=body.get('url'); provider=body.get('provider','manual')
    if not url: raise HTTPException(400,'url required')
    try: sel=await resolver.override(item_id,art_type,url,provider)
    except Exception as exc: raise HTTPException(400,str(exc))
    return dict(sel)
