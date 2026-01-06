from fastapi import APIRouter, Request, Response, Depends, Query
from fastapi.responses import RedirectResponse, JSONResponse
import httpx
import json
import os
import hashlib
import secrets
import urllib.parse
import asyncio
import itertools
import unicodedata
import re
from sqlalchemy.orm import Session
from database import UserSettings, User, get_db
from routers.globals import (
    get_db_nav_auth_params, 
    REGION, 
    ATMOS_TOKEN_FILE, 
    get_tidal_request, 
    get_current_user,
    create_access_token 
)
from routers.subsonic import subsonic_get_cover_art, subsonic_stream

router = APIRouter(prefix="/api")
token_cache = {}

def normalize_str(s):
    if not s: return ""
    return unicodedata.normalize('NFKD', str(s)).encode('ASCII', 'ignore').decode('utf-8').lower().strip()

def is_same_string(s1, s2):
    return normalize_str(s1) == normalize_str(s2)

def get_tidal_token_safe(settings: UserSettings):
    if settings and settings.tidal_access_token:
        return settings.tidal_access_token
    return None

def get_tidal_image_url(uuid: str, size: int = None):
    if not uuid: return None
    path = uuid.replace("-", "/")
    best_size = 320
    return f"https://resources.tidal.com/images/{path}/320x320.jpg"

async def get_real_nav_session(settings: UserSettings, username: str, force_refresh=False):
    if not settings or not settings.nav_url: return None
    
    nav_user = settings.nav_username or username
    user_id_prefix = str(settings.user_id) if hasattr(settings, 'user_id') else "default"
    cache_key = f"{user_id_prefix}_{nav_user}"

    if not force_refresh and cache_key in token_cache:
        return token_cache[cache_key]["token"]

    password = settings.nav_password
    if not password: return None

    base_url = settings.nav_url.rstrip('/')

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base_url}/auth/login", 
                json={"username": nav_user, "password": password}
            )
            if resp.status_code == 200:
                token = resp.json().get("token")
                if token:
                    token_cache[cache_key] = {"token": token}
                    return token
    except: pass
    return None

async def fetch_real_nav(endpoint: str, request: Request, settings: UserSettings, params_override=None):
    if settings is None or not settings.nav_url:
        return [], "0"

    username = settings.nav_username or "admin"
    token = await get_real_nav_session(settings, username)
    
    headers = {}
    if token:
        headers["x-nd-authorization"] = f"Bearer {token}"
        headers["Authorization"] = f"Bearer {token}"

    if params_override is not None:
        params = params_override
    else:
        params = dict(request.query_params)

    if 'jwt' in params: params.pop('jwt') 
    
    base_url = settings.nav_url.rstrip('/')

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base_url}/api/{endpoint}", params=params, headers=headers)
            if resp.status_code == 200:
                return resp.json(), resp.headers.get("X-Total-Count", "0")
            elif resp.status_code == 401:
                new_token = await get_real_nav_session(settings, username, force_refresh=True)
                if new_token:
                    headers["x-nd-authorization"] = f"Bearer {new_token}"
                    headers["Authorization"] = f"Bearer {new_token}"
                    resp = await client.get(f"{base_url}/api/{endpoint}", params=params, headers=headers)
                    if resp.status_code == 200: 
                        return resp.json(), resp.headers.get("X-Total-Count", "0")
            else:
                print(f"[NavFetch Error] Endpoint: {endpoint} | Status: {resp.status_code} | URL: {resp.url}")
    except Exception as e:
        print(f"Nav fetch error: {e}")
    return [], "0"

def create_list_response(data: list, total_count: int, start: int = 0, end: int = None):
    count_in_page = len(data)
    if total_count == 0 or count_in_page == 0:
        range_str = "items */0"
    else:
        current_end_index = start + count_in_page - 1
        range_str = f"items {start}-{current_end_index}/{total_count}"
        
    headers = {
        "X-Total-Count": str(total_count),
        "x-total-count": str(total_count),
        "Content-Range": range_str,
        "Access-Control-Expose-Headers": "X-Total-Count, Content-Range"
    }
    return Response(content=json.dumps(data), media_type="application/json", headers=headers)

def get_settings_safe(current_user: str, db: Session):
    if not current_user: return None
    user = db.query(User).filter(User.username == current_user).first()
    if user and user.settings and user.settings.nav_url:
        return user.settings
    if current_user == "admin":
         admin = db.query(User).filter(User.username == "admin").first()
         if admin: return admin.settings
    return None

@router.post("/auth/login")
async def navidrome_auth_login(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except:
        return Response(status_code=400)
        
    username = body.get("username")
    password = body.get("password")
    
    if not username or not password:
         return Response(content=json.dumps({"error": "Missing credentials"}), status_code=401, media_type="application/json")

    user = db.query(User).filter(User.username == username).first()
    
    valid_login = False
    if user:
         if user.hashed_password == password: 
             valid_login = True
    else:
         if username == "admin" and password == "password":
             valid_login = True

    if valid_login:
        token = create_access_token(data={"sub": username})
        return {
            "id": user.id if user else "admin",
            "username": username,
            "token": token,
            "isAdmin": True,
            "name": username,
            "email": f"{username}@example.com",
            "image": None
        }
    
    return Response(content=json.dumps({"error": "Invalid credentials"}), status_code=401, media_type="application/json")

async def find_tidal_artist_id(name: str, settings: UserSettings):
    token = get_tidal_token_safe(settings)
    if not token: return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        q = urllib.parse.quote(name)
        url = f"https://api.tidal.com/v1/search/artists?query={q}&limit=3&countryCode={REGION}"
        resp = await get_tidal_request(url, headers)
        if resp and resp.status_code == 200:
            items = resp.json().get("items", [])
            for item in items:
                if is_same_string(item.get("name"), name):
                    return str(item.get("id"))
    except: pass
    return None

async def find_nav_artist_id(name: str, request: Request, settings: UserSettings):
    try:
        filter_json = json.dumps({"name": name})
        api_params = {"_filter": filter_json, "_end": 10} 
        data, _ = await fetch_real_nav("artist", request, settings, params_override=api_params)
        if isinstance(data, list):
            for item in data:
                if is_same_string(item.get("name"), name):
                    return item.get("id")
    except: pass
    return None

async def fetch_tidal_artist_details(tidal_id: str, settings: UserSettings):
    token = get_tidal_token_safe(settings)
    if not token: return None
    headers = {"Authorization": f"Bearer {token}"}
    clean_id = tidal_id.replace("tidal_", "")
    try:
        t_art = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}?countryCode={REGION}", headers)
        t_alb = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}/albums?countryCode={REGION}&limit=100", headers)
        t_sim = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}/similar?countryCode={REGION}&limit=10", headers)
        r_art, r_alb, r_sim = await asyncio.gather(t_art, t_alb, t_sim, return_exceptions=True)
        
        if not r_art or isinstance(r_art, Exception) or r_art.status_code != 200: return None
        info = r_art.json()
        pic = info.get("picture")
        pic_val = get_tidal_image_url(pic, 320) if pic else "" 
        cover_id = f"tidal_{pic}" if pic else f"tidal_{info.get('id')}"
        
        albums = []
        if r_alb and not isinstance(r_alb, Exception) and r_alb.status_code == 200:
            for a in r_alb.json().get("items", []):
                cov = a.get("cover")
                cov_val = f"tidal_{cov}" if cov else f"tidal_{a.get('id')}"
                alb_title = f"(T) {a.get('title')}"
                albums.append({
                    "id": f"tidal_{a.get('id')}", "name": alb_title, "title": alb_title,
                    "artist": f"(T) {info.get('name')}", "artistId": f"tidal_{info.get('id')}",
                    "coverArt": cov_val, "year": int(a.get("releaseDate", "2000")[:4]) if a.get("releaseDate") else 2000,
                    "songCount": int(a.get("numberOfTracks", 10)), "isDir": True,
                    "created": a.get("releaseDate", "2023-01-01T00:00:00Z")
                })
        
        similar = []
        if r_sim and not isinstance(r_sim, Exception) and r_sim.status_code == 200:
            for s in r_sim.json().get("items", []):
                s_pic = s.get("picture")
                s_cover_id = f"tidal_{s_pic}" if s_pic else f"tidal_{s.get('id')}"
                s_pic_url = get_tidal_image_url(s_pic, 320) if s_pic else ""
                similar.append({
                    "id": f"tidal_{s.get('id')}", "name": f"(T) {s.get('name')}",
                    "albumCount": 0, "coverArt": s_cover_id, "artistImageUrl": s_pic_url
                })
        
        return {
            "id": f"tidal_{info.get('id')}", "name": f"(T) {info.get('name')}", "biography": "Tidal Artist",
            "coverArt": cover_id, "largeImageUrl": pic_val, "artistImageUrl": pic_val, "smallImageUrl": pic_val, "mediumImageUrl": pic_val,
            "albumCount": len(albums), "albums": albums, "similarArtists": similar
        }
    except: pass
    return None

async def fetch_nav_artist_details(nav_id: str, request: Request, settings: UserSettings):
    try:
        real_id = nav_id.replace("nav_", "") 
        real_id = re.sub(r'_\d+$', '', real_id)
        real_data, _ = await fetch_real_nav(f"artist/{real_id}", request, settings)
        if real_data:
            if "id" in real_data and not str(real_data["id"]).startswith("nav_"):
                 real_data["id"] = f"nav_{real_data['id']}"
            if "similarArtists" in real_data:
                cleaned_similar = []
                for s in real_data["similarArtists"]:
                    original_id = str(s.get("id", ""))
                    if original_id and not original_id.startswith("nav_"):
                        s["id"] = f"nav_{original_id}"
                        cleaned_similar.append(s)
                real_data["similarArtists"] = cleaned_similar
            if "albums" in real_data and isinstance(real_data["albums"], list):
                for alb in real_data["albums"]:
                    if "id" in alb and not str(alb["id"]).startswith("nav_"):
                        alb["id"] = f"nav_{alb['id']}"
                    if "artistId" in alb and not str(alb["artistId"]).startswith("nav_"):
                        alb["artistId"] = f"nav_{alb['artistId']}"
                    if "coverArt" in alb and not str(alb["coverArt"]).startswith("nav_"):
                        alb["coverArt"] = f"nav_{alb['coverArt']}"
                    elif "id" in alb:
                        alb["coverArt"] = alb["id"]
            return real_data
    except: pass
    return None

@router.get("/playlist")
async def navidrome_get_playlists(
    request: Request,
    current_user: str = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    settings = get_settings_safe(current_user, db)
    real_data, total = await fetch_real_nav("playlist", request, settings)
    
    normalized = []
    for p in real_data:
        normalized.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "comment": p.get("comment"),
            "owner": p.get("owner"),
            "songCount": p.get("songCount"),
            "duration": p.get("duration"),
            "created": p.get("created")
        })

    return create_list_response(normalized, total)

@router.get("/playlist/{id}")
async def navidrome_get_playlist_detail(id: str, request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    real_id = id.replace("nav_", "")
    real_id = re.sub(r'_\d+$', '', real_id)
    real_data, _ = await fetch_real_nav(f"playlist/{real_id}", request, settings)
    if real_data: return Response(content=json.dumps(real_data), media_type="application/json")
    return Response(status_code=404)

@router.get("/playlist/{id}/tracks")
async def navidrome_get_playlist_tracks(id: str, request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    real_id = id.replace("nav_", "")
    real_id = re.sub(r'_\d+$', '', real_id)
    real_data, total = await fetch_real_nav(f"playlist/{real_id}/tracks", request, settings)
    start = int(request.query_params.get("_start", 0))
    
    if real_data:
        for item in real_data:
            if "id" in item and not str(item["id"]).startswith("nav_"):
                item["id"] = f"nav_{item['id']}"
            if "artistId" in item and not str(item["artistId"]).startswith("nav_"):
                item["artistId"] = f"nav_{item['artistId']}"
            if "albumId" in item and not str(item["albumId"]).startswith("nav_"):
                item["albumId"] = f"nav_{item['albumId']}"
                
    if real_data is not None:
        return create_list_response(real_data, int(total), start)
    return Response(content="[]", media_type="application/json")

@router.get("/artist")
async def navidrome_get_artists(request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    real_list, total = await fetch_real_nav("artist", request, settings)
    flat_artists = []
    if real_list and isinstance(real_list, list):
        for artist in real_list:
            if "id" in artist and not str(artist["id"]).startswith("nav_"):
                artist["id"] = f"nav_{artist['id']}"
            flat_artists.append(artist)
            
    return create_list_response(flat_artists, len(flat_artists))

@router.get("/artist/{id}")
async def navidrome_get_artist_detail(id: str, request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    is_tidal_req = "tidal_" in id or (id.isdigit() and len(id) < 15 and not id.startswith("nav_"))
    
    primary_data = None
    secondary_data = None
    
    if is_tidal_req:
        tidal_id = id
        primary_data = await fetch_tidal_artist_details(tidal_id, settings)
        if primary_data:
            artist_name = primary_data.get("name").replace("(T) ", "")
            nav_id = await find_nav_artist_id(artist_name, request, settings)
            if nav_id:
                secondary_data = await fetch_nav_artist_details(f"nav_{nav_id}", request, settings)
    else:
        nav_id = id
        primary_data = await fetch_nav_artist_details(nav_id, request, settings)
        if primary_data:
            artist_name = primary_data.get("name")
            tidal_real_id = await find_tidal_artist_id(artist_name, settings)
            if tidal_real_id:
                secondary_data = await fetch_tidal_artist_details(f"tidal_{tidal_real_id}", settings)

    if not primary_data:
        return Response(status_code=404)

    base_obj = primary_data.copy()
    if is_tidal_req and base_obj.get("id") != id: base_obj["id"] = id
    if not is_tidal_req and not str(base_obj.get("id")).startswith("nav_"):
         base_obj["id"] = f"nav_{base_obj.get('id')}"

    fusion_albums = []
    if secondary_data: fusion_albums = secondary_data.get("albums", [])
    base_albums = base_obj.get("albums", [])
    if base_albums is None: base_albums = []
    
    existing_names = {normalize_str(a.get("name", "")) for a in base_albums}
    for fa in fusion_albums:
        if normalize_str(fa.get("name", "")) in existing_names: continue
        base_albums.append(fa)
    
    base_albums.sort(key=lambda x: (int(x.get("year") or 0), x.get("title") or ""), reverse=True)
    base_obj["albums"] = base_albums
    base_obj["albumCount"] = len(base_albums)
    
    return Response(content=json.dumps(base_obj), media_type="application/json")

@router.get("/album")
async def navidrome_get_albums(
    request: Request,
    _sort: str = "play_date",
    _order: str = "DESC",
    _start: int = 0,
    _end: int = 20,
    current_user: str = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    settings = get_settings_safe(current_user, db)
    
    params = dict(request.query_params)
    tidal_list = []
    
    search_query = params.get("name") or params.get("title") or params.get("q") or params.get("title_like")
    if not search_query and params.get("_filter"):
        try:
            f_json = json.loads(params.get("_filter"))
            if isinstance(f_json, dict):
                search_query = f_json.get("title") or f_json.get("name") or f_json.get("query")
        except: pass

    artist_id = params.get("artist_id")
    fetch_tidal_for_artist = False
    tid_to_fetch = None

    if artist_id and "tidal_" in artist_id:
        fetch_tidal_for_artist = True
        tid_to_fetch = artist_id.replace("tidal_", "")

    should_fetch_tidal = True

    if fetch_tidal_for_artist or should_fetch_tidal or not artist_id:
        try:
            tidal_token = get_tidal_token_safe(settings)
            if tidal_token:
                headers = {"Authorization": f"Bearer {tidal_token}"}
                url = ""
                
                if fetch_tidal_for_artist and tid_to_fetch:
                    url = f"https://api.tidal.com/v1/artists/{tid_to_fetch}/albums?countryCode={REGION}&limit=50"
                elif search_query:
                    q_str = urllib.parse.quote(str(search_query))
                    url = f"https://api.tidal.com/v1/search/albums?query={q_str}&limit=20&countryCode={REGION}"
                elif not artist_id:
                    url = f"https://api.tidal.com/v1/search/albums?query=New&limit=50&countryCode={REGION}"

                if url:
                    resp = await get_tidal_request(url, headers)
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        items = []
                        if "albums" in data and "items" in data["albums"]: items = data["albums"]["items"]
                        elif "items" in data: items = data["items"]
                        
                        for item in items:
                            alb = item.get("item", item)
                            if not alb.get("id"): continue
                            
                            art_id_raw = str(alb.get("artist", {}).get("id", "0"))
                            if fetch_tidal_for_artist and tid_to_fetch:
                                if art_id_raw != tid_to_fetch: continue

                            t_id = f"tidal_{alb.get('id')}"
                            art_name = alb.get("artist", {}).get("name", "Unknown")
                            art_id = f"tidal_{art_id_raw}"
                            cov_uuid = alb.get("cover")
                            cov_val = f"tidal_{cov_uuid}" if cov_uuid else t_id 
                            
                            final_artist_id = art_id
                            final_album_artist_id = art_id
                            if should_fetch_tidal and artist_id:
                                    final_artist_id = artist_id 
                                    final_album_artist_id = artist_id

                            alb_title = f"(T) {alb.get('title')}"
                            tidal_list.append({
                                "id": t_id,
                                "name": alb_title, "title": alb_title,
                                "artist": f"(T) {art_name}", 
                                "artistId": final_artist_id,
                                "albumArtist": f"(T) {art_name}", 
                                "albumArtistId": final_album_artist_id,
                                "coverArt": cov_val, 
                                "year": int(alb.get("releaseDate", "2000")[:4]) if alb.get("releaseDate") else 2000,
                                "songCount": int(alb.get("numberOfTracks", 10)),
                                "created": alb.get("releaseDate", "2023-01-01T00:00:00Z"),
                                "genre": "Tidal", "isDir": True
                            })
        except: pass

    if fetch_tidal_for_artist:
         return create_list_response(tidal_list, len(tidal_list))
    real_list = []
    real_nav_params = dict(params)
    
    if real_nav_params.get("artist_id"):
        real_nav_params["artist_id"] = real_nav_params["artist_id"].replace("nav_", "")
        real_nav_params["artist_id"] = re.sub(r'_\d+$', '', real_nav_params["artist_id"])
    
    is_artist_view = bool(real_nav_params.get("artist_id"))
    
    if is_artist_view:
        real_nav_params.pop("_start", None)
        real_nav_params.pop("_end", None)
        real_nav_params["_end"] = 10000 
    real_list, total_str = await fetch_real_nav("album", request, settings, params_override=real_nav_params)
    local_total = int(total_str) if total_str else 0
    if not isinstance(real_list, list): real_list = []
    
    for item in real_list:
        if "id" in item: item["id"] = f"nav_{item['id']}"
        if "artistId" in item: item["artistId"] = f"nav_{item['artistId']}"
        if "albumArtistId" in item: item["albumArtistId"] = f"nav_{item['albumArtistId']}"
        if "coverArt" in item and not str(item["coverArt"]).startswith("nav_"):
             item["coverArt"] = f"nav_{item['coverArt']}"

    if is_artist_view and should_fetch_tidal and artist_id:
        artist_name = None
        if real_list: artist_name = real_list[0].get("artist")
        if not artist_name:
             clean_aid = artist_id.replace("nav_", "")
             clean_aid = re.sub(r'_\d+$', '', clean_aid)
             art_info, _ = await fetch_real_nav(f"artist/{clean_aid}", request, settings)
             if art_info: artist_name = art_info.get("name")
        
        if artist_name:
            tid = await find_tidal_artist_id(artist_name, settings)
            if tid:
                try:
                    tidal_token = get_tidal_token_safe(settings)
                    headers = {"Authorization": f"Bearer {tidal_token}"}
                    url = f"https://api.tidal.com/v1/artists/{tid}/albums?countryCode={REGION}&limit=50"
                    resp = await get_tidal_request(url, headers)
                    if resp and resp.status_code == 200:
                        existing_titles = {normalize_str(a.get("name", "")) for a in real_list}
                        for item in resp.json().get("items", []):
                            if str(item.get("artist", {}).get("id")) != str(tid): continue
                            
                            if normalize_str(item.get("title", "")) not in existing_titles:
                                cov = item.get("cover")
                                cov_val = f"tidal_{cov}" if cov else f"tidal_{item.get('id')}"
                                alb_title = f"(T) {item.get('title')}"
                                tidal_list.append({
                                    "id": f"tidal_{item.get('id')}", "name": alb_title, "title": alb_title,
                                    "artist": f"(T) {item.get('artist', {}).get('name')}",
                                    "artistId": artist_id, 
                                    "albumArtist": f"(T) {item.get('artist', {}).get('name')}",
                                    "albumArtistId": artist_id,
                                    "coverArt": cov_val, "year": int(item.get("releaseDate", "2000")[:4]) if item.get("releaseDate") else 2000,
                                    "songCount": int(item.get("numberOfTracks", 10)), "genre": "Tidal", "isDir": True,
                                    "created": item.get("releaseDate", "2023-01-01T00:00:00Z")
                                })
                except: pass

    combined = []
    final_total = 0
    
    if is_artist_view:
        combined = real_list + tidal_list
        combined.sort(key=lambda x: (int(x.get("year") or 0), x.get("title") or ""), reverse=True)
        final_total = len(combined)
        
        req_start = int(params.get("_start", 0))
        param_end = params.get("_end")
        if param_end and int(param_end) > 0:
             req_end = int(param_end)
        else:
             req_end = req_start + 20

        sliced_data = combined[req_start:req_end]
        return create_list_response(sliced_data, final_total, req_start)
    else:
        for local, remote in itertools.zip_longest(real_list, tidal_list):
            if local: combined.append(local)
            if remote: combined.append(remote)
        
        final_total = local_total + len(tidal_list)
        return create_list_response(combined, final_total, int(params.get("_start", 0)))

@router.get("/album/{id}")
async def navidrome_get_album_detail(id: str, request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    
    if "tidal_" in id:
        try:
             clean_id = id.replace("tidal_", "")
             tidal_token = get_tidal_token_safe(settings)
             
             if not tidal_token:
                 return Response(status_code=404)

             headers = {"Authorization": f"Bearer {tidal_token}"}
             
             resp = await get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}?countryCode={REGION}", headers)
             tracks_resp = await get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}/items?countryCode={REGION}&limit=100", headers)

             if resp.status_code == 200:
                 alb = resp.json()
                 art_name = alb.get("artist", {}).get("name", "Unknown")
                 art_id = f"tidal_{alb.get('artist', {}).get('id', '0')}"
                 cover_uuid = alb.get("cover")
                 cover_val = f"tidal_{cover_uuid}" if cover_uuid else f"tidal_{alb.get('id')}"

                 songs_list = []
                 if tracks_resp.status_code == 200:
                     for item in tracks_resp.json().get("items", []):
                         song = item.get("item", item)
                         if song.get("type") != "TRACK": continue
                         
                         t_num = song.get("trackNumber", 1)
                         d_num = song.get("volumeNumber", 1)
                         
                         songs_list.append({
                            "id": f"tidal_{song.get('id')}",
                            "parent": f"tidal_{clean_id}", 
                            "isDir": False,
                            "title": song.get("title"),
                            "name": song.get("title"),
                            "album": alb.get("title"),
                            "albumId": f"tidal_{alb.get('id')}",
                            "artist": song.get("artist", {}).get("name", art_name),
                            "artistId": f"tidal_{song.get('artist', {}).get('id', '0')}",
                            "albumArtist": art_name,
                            "albumArtistId": art_id,
                            "trackNumber": t_num,
                            "track": t_num,
                            "discNumber": d_num,
                            "disc": d_num,
                            "year": int(song.get("streamStartDate", "2000")[:4]) if song.get("streamStartDate") else 2000,
                            "genre": "Tidal",
                            "coverArt": cover_val,
                            "size": 30000000,       
                            "contentType": "audio/flac",
                            "suffix": "flac",
                            "duration": song.get("duration", 0),
                            "bitRate": 1411,
                            "channels": 2,          
                            "sampleRate": 44100,    
                            "path": f"tidal/{song.get('id')}.flac",
                            "type": "music",
                            "plays": 0,
                            "playCount": 0
                         })

                 return Response(content=json.dumps({
                     "id": f"tidal_{alb.get('id')}",
                     "name": alb.get("title"),
                     "title": alb.get("title"),
                     "artist": art_name,
                     "artistId": art_id,
                     "albumArtist": art_name,
                     "albumArtistId": art_id,
                     "year": int(alb.get("releaseDate", "2000")[:4]) if alb.get("releaseDate") else 2000,
                     "coverArt": cover_val,
                     "songs": songs_list,
                     "songCount": len(songs_list),
                     "genre": "Tidal"
                 }), media_type="application/json")
        except Exception:
            pass

    real_id = id.replace("nav_", "")
    real_id = re.sub(r'_\d+$', '', real_id)
    real_data, _ = await fetch_real_nav(f"album/{real_id}", request, settings)
    if real_data:
        for key in ["artistId", "albumArtistId", "id"]:
            if key in real_data: real_data[key] = f"nav_{real_data[key]}"
        if "coverArt" in real_data and not str(real_data["coverArt"]).startswith("nav_"):
             real_data["coverArt"] = f"nav_{real_data['coverArt']}"
        
        track_list = real_data.get("tracks") or real_data.get("songs") or []
        if isinstance(track_list, list):
            for t in track_list:
                for key in ["id", "artistId", "albumId"]:
                    if key in t and not str(t[key]).startswith("nav_"):
                        t[key] = f"nav_{t[key]}"
                    
        return Response(content=json.dumps(real_data), media_type="application/json")
    return Response(status_code=404)

@router.get("/song")
async def navidrome_get_songs(request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings_safe(current_user, db)
    params = dict(request.query_params)
    album_id = params.get("album_id") or params.get("albumId")
    ids_filter = []
    
    if params.get("_filter"):
        try:
            f_json = json.loads(params.get("_filter"))
            if isinstance(f_json, dict):
                if not album_id:
                    album_id = f_json.get("album_id") or f_json.get("albumId")
                if "id" in f_json and isinstance(f_json["id"], list):
                    ids_filter = f_json["id"]
        except: pass

    is_tidal_req = False
    if album_id and "tidal_" in str(album_id):
        is_tidal_req = True
    elif ids_filter:
        for x in ids_filter:
            if isinstance(x, str) and "tidal_" in x:
                is_tidal_req = True
                break

    if is_tidal_req:
        tidal_token = get_tidal_token_safe(settings)
        if not tidal_token:
            return create_list_response([], 0)

        headers = {"Authorization": f"Bearer {tidal_token}"}
        songs_list = []
        
        try:
            items = []
            alb_info = {}
            
            if album_id and "tidal_" in str(album_id):
                clean_id = album_id.replace("tidal_", "")
                t1 = get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}?countryCode={REGION}", headers)
                t2 = get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}/items?countryCode={REGION}&limit=100", headers)
                r_alb, r_items = await asyncio.gather(t1, t2, return_exceptions=True)
                if not isinstance(r_alb, Exception) and r_alb.status_code == 200:
                    alb_info = r_alb.json()
                if not isinstance(r_items, Exception) and r_items.status_code == 200:
                    items = r_items.json().get("items", [])
            elif ids_filter:
                clean_ids = [i.replace("tidal_", "") for i in ids_filter if "tidal_" in i]
                if clean_ids:
                    ids_str = ",".join(clean_ids)
                    resp = await get_tidal_request(f"https://api.tidal.com/v1/tracks?ids={ids_str}&countryCode={REGION}", headers)
                    if resp and resp.status_code == 200:
                        raw_items = resp.json().get("items", [])
                        items = [{"item": i} for i in raw_items]

            if items:
                if not alb_info and items:
                    first = items[0].get("item") or items[0]
                    alb_info = first.get("album", {})
                
                alb_name = alb_info.get("title", "Unknown Album")
                cover_uuid = alb_info.get("cover")
                if not cover_uuid and album_id:
                     cover_uuid = album_id.replace("tidal_", "")
                cover_val = f"tidal_{cover_uuid}" if cover_uuid else f"tidal_{album_id}" if album_id else ""
                
                alb_artist_id = None
                if alb_info.get("artist") and alb_info.get("artist").get("id"):
                    alb_artist_id = f"tidal_{alb_info.get('artist').get('id')}"

                for item in items:
                    song = item.get("item", item)
                    if not song or (song.get("type") and song.get("type") != "TRACK"): continue
                    
                    art_name = song.get("artist", {}).get("name", "Unknown")
                    art_id = f"tidal_{song.get('artist', {}).get('id', '0')}"
                    t_num = song.get("trackNumber", 1)
                    d_num = song.get("volumeNumber", 1)
                    
                    if album_id:
                        this_alb_id = album_id
                    else:
                        this_alb_id = f"tidal_{song.get('album', {}).get('id')}"

                    current_alb_art_id = alb_artist_id if alb_artist_id else art_id

                    songs_list.append({
                        "id": f"tidal_{song.get('id')}",
                        "parent": this_alb_id, 
                        "isDir": False,
                        "title": song.get("title"),
                        "name": song.get("title"), 
                        "album": song.get("album", {}).get("title", alb_name),
                        "albumId": this_alb_id,
                        "artist": art_name,
                        "artistId": art_id,
                        "albumArtist": art_name,
                        "albumArtistId": current_alb_art_id,
                        "trackNumber": t_num,
                        "track": t_num,
                        "discNumber": d_num,
                        "disc": d_num,
                        "year": int(song.get("streamStartDate", "2000")[:4]) if song.get("streamStartDate") else 2000,
                        "genre": "Tidal",
                        "coverArt": cover_val,
                        "size": 30000000,       
                        "contentType": "audio/flac",
                        "suffix": "flac",
                        "duration": song.get("duration", 0),
                        "bitRate": 1411,
                        "channels": 2,          
                        "sampleRate": 44100,    
                        "path": f"tidal/{song.get('id')}.flac",
                        "type": "music",
                        "plays": 0,
                        "playCount": 0,
                        "created": song.get("streamStartDate", "2023-01-01T00:00:00Z")
                    })
        except Exception:
            return create_list_response([], 0)
            
        songs_list.sort(key=lambda x: (x.get("discNumber", 1), x.get("trackNumber", 1)))

        total_count = len(songs_list)
        start = int(params.get("_start", 0))
        end = params.get("_end")
        end_idx = int(end) if end is not None else None
        
        if end_idx is not None and end_idx > 0:
            paged_list = songs_list[start:end_idx]
        else:
            paged_list = songs_list[start:]

        return create_list_response(paged_list, total_count, start)

    nav_params = dict(params)
    
    for k in ["album_id", "albumId", "artist_id", "artistId", "id"]:
        if nav_params.get(k) and "nav_" in nav_params[k]: 
            nav_params[k] = nav_params[k].replace("nav_", "")
            nav_params[k] = re.sub(r'_\d+$', '', nav_params[k])
    
    if nav_params.get("_filter"):
        try:
             nav_params["_filter"] = nav_params["_filter"].replace("nav_", "")
             nav_params["_filter"] = re.sub(r'_\d+', '', nav_params["_filter"])
        except: pass

    real_data, total_count = await fetch_real_nav("song", request, settings, params_override=nav_params)
    
    if isinstance(real_data, list):
        for item in real_data:
            for key in ["id", "albumId", "artistId", "coverArt"]:
                val = str(item.get(key, ""))
                if val and not val.startswith("nav_") and not val.startswith("tidal_"):
                    item[key] = f"nav_{val}"
            
            if "type" not in item: item["type"] = "music"
            if "isVideo" not in item: item["isVideo"] = False

    start = int(params.get("_start", 0))
    return create_list_response(real_data, int(total_count), start)

@router.get("/coverArt")
async def navidrome_cover_art(id: str, size: int = 300, request: Request = None, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    return await subsonic_get_cover_art(id=id, size=size, u=current_user, db=db, request=request)

@router.get("/stream")
@router.head("/stream")
async def navidrome_stream(id: str, request: Request, current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    return await subsonic_stream(id=id, request=request, u=current_user, db=db)

@router.get("/getTopSongs")
@router.post("/getTopSongs")
async def mock_get_top_songs(request: Request):
    return Response(content=json.dumps({"subsonic-response": {"status": "ok", "version": "1.16.1", "topSongs": {"song": []}}}), media_type="application/json")