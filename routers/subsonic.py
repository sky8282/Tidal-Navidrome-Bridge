from fastapi import APIRouter, Depends, Query, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
import httpx
import asyncio
import time
import html
import json
import os
import base64
import hashlib
import secrets
import urllib.parse
import re
from asyncio import gather
from urllib.parse import quote, urlencode
from sqlalchemy.orm import Session
from database import get_db, User, UserSettings
from .globals import inject_subsonic_auth
from . import globals as global_vars
from .globals import (
    verify_subsonic_request,
    get_subsonic_tidal_token,
    create_subsonic_response,
    escape_xml,
    REGION,
    ATMOS_TOKEN_FILE,
    get_tidal_request
)

router = APIRouter(prefix="/rest")
tidal_lookup_limit = asyncio.Semaphore(5)

def get_settings_by_username(username: str, db: Session) -> UserSettings:
    if not username: return None
    if not hasattr(db, "query"): return None
    user = db.query(User).filter(User.username == username).first()
    if user and user.settings and user.settings.nav_url:
        return user.settings
    admin = db.query(User).filter(User.username == "admin").first()
    if admin and admin.settings:
        return admin.settings
    return None

def get_auth_params(settings: UserSettings):
    if not settings:
        return {"u": "error", "p": "error", "v": "1.16.1", "c": "TidalProxy", "f": "json"}
    user = settings.nav_username or "admin"
    pwd = settings.nav_password or ""
    if pwd:
        salt = secrets.token_hex(6)
        token = hashlib.md5((pwd + salt).encode('utf-8')).hexdigest()
        return {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "TidalProxy", "f": "json"}
    return {"u": user, "p": "admin", "v": "1.16.1", "c": "TidalProxy", "f": "json"}

def get_user_tidal_token(settings: UserSettings):
    if settings and settings.tidal_access_token:
        return settings.tidal_access_token
    return get_subsonic_tidal_token()

def safe_xml_id(val):
    s = str(val).strip()
    if not s or s == "None": return "0"
    return s

def get_tidal_image_url(uuid: str, size: int = None):
    print(f"[DEBUG] Generate URL for UUID: {uuid} | Size: {size}")
    
    if not uuid or uuid == "None": 
        print(f"[DEBUG] -> UUID is empty or None")
        return None
        
    if uuid.startswith("http"): return uuid
    if uuid.isdigit() and len(uuid) < 25: 
        print(f"[DEBUG] -> UUID is pure digit (invalid for image): {uuid}")
        return None

    clean = uuid.replace("-", "")
    
    if len(clean) != 32: 
        print(f"[DEBUG] -> UUID length invalid ({len(clean)}): {clean}")
        return None
    
    path = f"{clean[:8]}/{clean[8:12]}/{clean[12:16]}/{clean[16:20]}/{clean[20:]}"
    final_url = f"https://resources.tidal.com/images/{path}/320x320.jpg"
    print(f"[DEBUG] -> Final URL: {final_url}")
    return final_url

async def fetch_local_nav_artist(nav_id: str, settings: UserSettings):
    if not settings or not settings.nav_url: return None
    params = get_auth_params(settings)
    clean = nav_id.replace("nav_", "")
    clean = re.sub(r'_\d+$', '', clean)
    params["id"] = clean
    base_url = settings.nav_url.rstrip('/')
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/rest/getArtist.view", params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("subsonic-response", {}).get("artist")
    except: pass
    return None

async def fetch_local_nav_artist_info(nav_id: str, settings: UserSettings):
    if not settings or not settings.nav_url: return None
    params = get_auth_params(settings)
    clean = nav_id.replace("nav_", "")
    clean = re.sub(r'_\d+$', '', clean)
    params["id"] = clean
    base_url = settings.nav_url.rstrip('/')
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/rest/getArtistInfo2.view", params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("subsonic-response", {}).get("artistInfo2")
            else:
                resp = await client.get(f"{base_url}/rest/getArtistInfo.view", params=params)
                if resp.status_code == 200:
                     data = resp.json()
                     return data.get("subsonic-response", {}).get("artistInfo")
    except: pass
    return None

async def search_local_nav_id(name: str, settings: UserSettings):
    if not settings or not settings.nav_url: return None
    params = get_auth_params(settings)
    params["query"] = name
    params["artistCount"] = 1
    base_url = settings.nav_url.rstrip('/')
    try:
         async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/rest/search3.view", params=params)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("subsonic-response", {}).get("searchResult3", {}).get("artist", [])
                if items:
                    for item in items:
                        if item.get("name", "").lower() == name.lower():
                            return item.get("id")
    except: pass
    return None

async def search_tidal_id(name: str, tidal_token: str, ref_albums: list = None):
    if not tidal_token or not global_vars.shared_api_client: return None
    headers = {"authorization": f"Bearer {tidal_token}"}
    try:
        q = quote(str(name))
        url = f"https://api.tidal.com/v1/search/artists?query={q}&limit=3&countryCode={REGION}"
        resp = await get_tidal_request(url, headers)
        if resp and resp.status_code == 200:
            items = resp.json().get("artists", {}).get("items", [])
            for item in items:
                if item.get("name", "").lower() == name.lower():
                    return str(item.get("id"))
            if items: return str(items[0].get("id"))
    except: pass
    return None

def clean_tidal_bio(text):
    if not text: return ""
    return text.replace("TIDAL", "StreamService")
    
@router.api_route("/ping", methods=["GET", "POST"])
@router.api_route("/ping.view", methods=["GET", "POST"])
async def subsonic_ping(u: str = None, f: str = None, **kwargs):
    if f == "json": return JSONResponse(content=create_subsonic_response())
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"/>', media_type="application/xml")

@router.api_route("/getLicense", methods=["GET", "POST"])
@router.api_route("/getLicense.view", methods=["GET", "POST"])
async def subsonic_get_license(f: str = None, **kwargs):
    if f == "json": return JSONResponse(content=create_subsonic_response({"license": {"valid": True, "email": "user@example.com", "licenseExpires": "2099-12-31T23:59:59"}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><license valid="true" email="user@example.com" licenseExpires="2099-12-31T23:59:59"/></subsonic-response>', media_type="application/xml")

@router.api_route("/getUser", methods=["GET", "POST"])
@router.api_route("/getUser.view", methods=["GET", "POST"])
async def subsonic_get_user(u: str = None, f: str = None, **kwargs):
    username = u if u else "admin"
    if f == "json": return JSONResponse(content=create_subsonic_response({"user": {"username": username, "email": "admin@tidal-proxy.com", "scrobblingEnabled": True, "adminRole": True, "streamRole": True}}))
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><user username="{username}" email="admin@tidal-proxy.com" scrobblingEnabled="true" adminRole="true" streamRole="true"/></subsonic-response>', media_type="application/xml")

@router.get("/getMusicFolders")
@router.get("/getMusicFolders.view")
async def subsonic_get_music_folders(
    u: str = Query(None), 
    f: str = None, 
    **kwargs
):
    if f == "json": return JSONResponse(content=create_subsonic_response({"musicFolders": {"musicFolder": [{"id": "1", "name": "Tidal"}]}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><musicFolders><musicFolder id="1" name="Tidal"/></musicFolders></subsonic-response>', media_type="application/xml")

@router.get("/getArtist")
@router.get("/getArtist.view")
async def subsonic_get_artist(
    id: str, 
    u: str = Query(None), 
    db: Session = Depends(get_db), 
    size: int = None, count: int = None, offset: int = 0, f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)
    
    run_tidal = True if tidal_token else False
    is_explicit_tidal = "tidal_" in id
    is_explicit_nav = "nav_" in id
    is_tidal_req = is_explicit_tidal or (id.isdigit() and not is_explicit_nav)
    
    primary_data = None
    fusion_albums = []
    
    if is_tidal_req:
        clean_t_id = id.replace("tidal_", "")
        if tidal_token and global_vars.shared_api_client:
             headers = {"authorization": f"Bearer {tidal_token}"}
             try:
                t_art = await get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_t_id}?countryCode={REGION}", headers)
                if t_art and t_art.status_code == 200:
                    info = t_art.json()
                    pic = info.get("picture")
                    pic_url = get_tidal_image_url(pic, 320) if pic else "" 
                    cover_val = f"tidal_{pic}" if pic else f"tidal_{info.get('id')}"
                    
                    primary_data = {
                        "id": f"tidal_{info.get('id')}", "name": f"(T) {info.get('name')}",
                        "coverArt": cover_val, "artistImageUrl": pic_url, "largeImageUrl": pic_url, "album": []
                    }
                    
                    t_alb = await get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_t_id}/albums?countryCode={REGION}&limit=50", headers)
                    if t_alb and t_alb.status_code == 200:
                        for a in t_alb.json().get("items", []):
                            cov = a.get("cover")
                            cov_val = f"tidal_{cov}" if cov else f"tidal_{a.get('id')}"
                            alb_title = f"(T) {a.get('title')}" or "(T) Unknown"
                            primary_data["album"].append({
                                "id": f"tidal_{a.get('id')}", "name": alb_title, "title": alb_title, "coverArt": cov_val,
                                "songCount": int(a.get("numberOfTracks", 10)), "year": int(a.get("releaseDate", "2000")[:4]) if a.get("releaseDate") else 2000,
                                "created": a.get("releaseDate", "2000-01-01"), "artist": primary_data["name"], "artistId": primary_data["id"], "isDir": True
                            })
                    
                    if settings:
                        l_id = await search_local_nav_id(info.get("name"), settings)
                        if l_id:
                            loc_art = await fetch_local_nav_artist(l_id, settings)
                            if loc_art:
                                loc_albs = loc_art.get("album", [])
                                if isinstance(loc_albs, dict): loc_albs = [loc_albs]
                                for la in loc_albs:
                                    if "nav_" not in str(la.get("id")): la["id"] = f"nav_{la['id']}"
                                    fusion_albums.extend([la])
             except: pass
    
    else:
        if not settings:
             error_msg = "User not configured"
             if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 10, "message": error_msg}}))
             return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="10" message="{error_msg}"/></subsonic-response>', media_type="application/xml")

        nav_id = id.replace("nav_", "")
        primary_data = await fetch_local_nav_artist(nav_id, settings)
        if primary_data:
            if "nav_" not in str(primary_data.get("id")): primary_data["id"] = f"nav_{primary_data['id']}"
            
            if not primary_data.get("artistImageUrl"):
                 primary_data["artistImageUrl"] = primary_data.get("largeImageUrl") or primary_data.get("mediumImageUrl") or ""
            
            if "album" in primary_data:
                raw_albums = primary_data["album"]
                if isinstance(raw_albums, dict): raw_albums = [raw_albums]
                processed_albums = []
                for alb in raw_albums:
                    if "id" in alb and not str(alb["id"]).startswith("nav_"):
                        alb["id"] = f"nav_{alb['id']}"
                    if "artistId" in alb and not str(alb["artistId"]).startswith("nav_"):
                        alb["artistId"] = f"nav_{alb['artistId']}"
                    if "coverArt" in alb and not str(alb["coverArt"]).startswith("nav_"):
                         alb["coverArt"] = f"nav_{alb['coverArt']}"
                    processed_albums.append(alb)
                primary_data["album"] = processed_albums

            if run_tidal:
                 t_id = await search_tidal_id(primary_data.get("name"), tidal_token)
                 if t_id and global_vars.shared_api_client:
                      headers = {"authorization": f"Bearer {tidal_token}"}
                      try:
                          t_alb = await get_tidal_request(f"https://api.tidal.com/v1/artists/{t_id}/albums?countryCode={REGION}&limit=20", headers)
                          if t_alb and t_alb.status_code == 200:
                                for a in t_alb.json().get("items", []):
                                    cov = a.get("cover")
                                    cov_val = f"tidal_{cov}" if cov else f"tidal_{a.get('id')}"
                                    alb_title = f"(T) {a.get('title')}" or "(T) Unknown"
                                    fusion_albums.append({
                                        "id": f"tidal_{a.get('id')}", "name": alb_title, "title": alb_title, "coverArt": cov_val,
                                        "songCount": int(a.get("numberOfTracks", 10)), "year": int(a.get("releaseDate", "2000")[:4]) if a.get("releaseDate") else 2000,
                                        "created": a.get("releaseDate", "2000-01-01"), "artist": f"(T) {a.get('artist', {}).get('name')}",
                                        "artistId": f"tidal_{t_id}", "isDir": True
                                    })
                      except: pass

    if not primary_data:
         error_msg = "Artist not found"
         if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": error_msg}}))
         return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="70" message="{error_msg}"/></subsonic-response>', media_type="application/xml")

    base_albums = primary_data.get("album", [])
    if isinstance(base_albums, dict): base_albums = [base_albums]
    existing_titles = {a.get("name", "").lower() for a in base_albums}
    final_albums = list(base_albums)
    for fa in fusion_albums:
        if fa.get("name", "").lower() not in existing_titles:
            final_albums.append(fa)
    final_albums.sort(key=lambda x: x.get("year", 0), reverse=True)
    primary_data["album"] = final_albums
    primary_data["albumCount"] = len(final_albums)
    
    if f == "json": return JSONResponse(content=create_subsonic_response({"artist": primary_data}))
    
    xml_albums = ""
    for a in final_albums:
        s_id = safe_xml_id(a.get("id"))
        s_name = escape_xml(a.get("name") or a.get("title"))
        s_artist = escape_xml(a.get("artist"))
        s_artist_id = safe_xml_id(a.get("artistId"))
        xml_albums += f'<album id="{s_id}" name="{s_name}" title="{s_name}" coverArt="{a.get("coverArt") or ""}" songCount="{a.get("songCount")}" created="{a.get("created", "")}" artist="{s_artist}" artistId="{s_artist_id}" year="{a.get("year")}" isDir="true"/>'

    xml_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1">
    <artist id="{safe_xml_id(primary_data.get("id"))}" name="{escape_xml(primary_data.get("name"))}" coverArt="{primary_data.get("coverArt") or ""}" artistImageUrl="{escape_xml(primary_data.get("artistImageUrl") or "")}" albumCount="{primary_data.get("albumCount")}">
        {xml_albums}
    </artist>
</subsonic-response>'''
    return Response(content=xml_content, media_type="application/xml")

@router.get("/getCoverArt")
@router.get("/getCoverArt.view")
async def subsonic_get_cover_art(
    id: str, 
    u: str = Query(None), 
    db: Session = Depends(get_db), 
    size: int = None, 
    f: str = None, 
    request: Request = None, 
    **kwargs
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    def get_smart_size(s):
        if not s: return 320 
        s = int(s)
        if s <= 80: return 80
        if s <= 160: return 160
        if s <= 320: return 320
        if s <= 640: return 640
        return 1280

    def make_tidal_url(uuid, s):
        if not uuid: return None
        if str(uuid).startswith("http"): return uuid
        
        clean = str(uuid).replace("-", "")
        if len(clean) != 32: return None 
        
        path = f"{clean[:8]}/{clean[8:12]}/{clean[12:16]}/{clean[16:20]}/{clean[20:]}"
        final_size = get_smart_size(s)
        return f"https://resources.tidal.com/images/{path}/320x320.jpg"

    raw_id = str(id)

    clean_step_1 = raw_id
    is_artist_prefix = False
    
    for prefix in ["song-", "al-", "ar-"]:
        if clean_step_1.startswith(prefix):
            if prefix == "ar-": is_artist_prefix = True
            clean_step_1 = clean_step_1[len(prefix):]
            break

    is_nav_source = clean_step_1.startswith("nav_")
    is_tidal_source = clean_step_1.startswith("tidal_")
    clean_step_2 = clean_step_1
    if is_nav_source: clean_step_2 = clean_step_2.replace("nav_", "")
    if is_tidal_source: clean_step_2 = clean_step_2.replace("tidal_", "")
    common_id = re.sub(r'_\d+$', '', clean_step_2)
    is_pure_digit = common_id.isdigit()
    allow_tidal_lookup = (is_tidal_source or is_pure_digit) and not is_nav_source

    if allow_tidal_lookup:
        if tidal_token and global_vars.shared_api_client:
            headers = {"authorization": f"Bearer {tidal_token}"}
            async def fetch_tidal_cover(entity_type, tid):
                async with tidal_lookup_limit: 
                    try:
                        url = f"https://api.tidal.com/v1/{entity_type}/{tid}?countryCode={REGION}"
                        r = await get_tidal_request(url, headers)
                        if r and r.status_code == 200:
                            data = r.json()
                            if entity_type == "artists": return data.get("picture")
                            if entity_type == "albums": return data.get("cover")
                            if entity_type == "playlists": return data.get("squareImage")
                            if entity_type == "mixes": return data.get("graphics", {}).get("images", [{}])[0].get("id")
                            if entity_type == "tracks": return data.get("album", {}).get("cover")
                    except: pass
                return None

            tasks = []
            if is_artist_prefix:
                tasks.append(fetch_tidal_cover("artists", common_id))
            else:
                tasks.append(fetch_tidal_cover("albums", common_id))
                tasks.append(fetch_tidal_cover("tracks", common_id))
                tasks.append(fetch_tidal_cover("playlists", common_id))
                tasks.append(fetch_tidal_cover("artists", common_id))
            results = await asyncio.gather(*tasks)
            resolved_img_uuid = next((res for res in results if res), None)
            if resolved_img_uuid:
                final_url = make_tidal_url(resolved_img_uuid, size)
                if final_url: return RedirectResponse(url=final_url, status_code=302)
        clean_uuid_check = common_id.replace("-", "")
        is_valid_uuid = len(clean_uuid_check) == 32 and all(c in "0123456789abcdefABCDEF" for c in clean_uuid_check)
        
        if is_valid_uuid and not is_artist_prefix and is_tidal_source:
            direct_url = make_tidal_url(common_id, size)
            if direct_url: return RedirectResponse(url=direct_url, status_code=302)

    if settings and settings.nav_url:
        params = get_auth_params(settings)
        fallback_id = raw_id
        if fallback_id.startswith("nav_"): fallback_id = fallback_id[4:]
        if fallback_id.startswith("tidal_"): fallback_id = fallback_id[6:]
        fallback_id = re.sub(r'_\d+$', '', fallback_id)
        params["id"] = fallback_id
        if size: params["size"] = size
        base_url = settings.nav_url.rstrip('/')
        query = urlencode(params)
        return RedirectResponse(f"{base_url}/rest/getCoverArt.view?{query}", status_code=302)
    return Response(status_code=404)

@router.get("/getArtistInfo")
@router.get("/getArtistInfo.view")
@router.get("/getArtistInfo2")
@router.get("/getArtistInfo2.view")
async def subsonic_get_artist_info(
    id: str, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    count: int = 20, f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)
    is_explicit_tidal = "tidal_" in id
    is_pure_tidal = (id.isdigit() and not "nav_" in id)
    is_tidal_req = is_explicit_tidal or is_pure_tidal
    final_bio = ""
    final_image = ""
    final_similar = []
    
    if is_tidal_req:
        clean_id = id.replace("tidal_", "")
        if tidal_token and global_vars.shared_api_client:
            headers = {"authorization": f"Bearer {tidal_token}"}
            try:
                t1 = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}/bio?countryCode={REGION}", headers)
                t2 = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}?countryCode={REGION}", headers)
                t3 = get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}/similar?countryCode={REGION}&limit={count}", headers)
                r_bio, r_info, r_sim = await gather(t1, t2, t3, return_exceptions=True)
                
                if r_bio and not isinstance(r_bio, Exception) and r_bio.status_code == 200:
                    raw_text = r_bio.json().get("text", "")
                    final_bio = clean_tidal_bio(raw_text)
                if r_info and not isinstance(r_info, Exception) and r_info.status_code == 200:
                    pic = r_info.json().get("picture")
                    if pic: final_image = get_tidal_image_url(pic, 320)
                if r_sim and not isinstance(r_sim, Exception) and r_sim.status_code == 200:
                    for item in r_sim.json().get("items", []):
                        sim_pic = item.get("picture", "")
                        sim_pic_val = f"tidal_{sim_pic}" if sim_pic else f"tidal_{item.get('id')}"
                        final_similar.append({
                            "id": f"tidal_{item.get('id')}", "name": f"(T) {item.get('name')}",
                            "coverArt": sim_pic_val, "artistImageUrl": get_tidal_image_url(sim_pic, 320) if sim_pic else "",
                        })
            except: pass
    else:
        clean_id = id.replace("nav_", "")
        clean_id = re.sub(r'_\d+$', '', clean_id)
        local_info = await fetch_local_nav_artist_info(clean_id, settings)
        
        if local_info:
            final_bio = local_info.get("biography", "")
            final_image = local_info.get("largeImageUrl") or local_info.get("mediumImageUrl") or local_info.get("artistImageUrl") or ""
            loc_sims = local_info.get("similarArtist", [])
            if isinstance(loc_sims, dict): loc_sims = [loc_sims]
            for s in loc_sims:
                if "nav_" not in str(s.get("id")): s["id"] = f"nav_{s.get('id')}"
                if "coverArt" not in s and "id" in s: s["coverArt"] = s["id"]
                final_similar.append(s)

    data_payload = {
        "biography": final_bio, "musicBrainzId": "", "lastFmUrl": "",
        "largeImageUrl": final_image, "mediumImageUrl": final_image, "smallImageUrl": final_image, "artistImageUrl": final_image,
        "similarArtist": final_similar
    }
    
    if f == "json":
        return JSONResponse(content=create_subsonic_response({"artistInfo": data_payload, "artistInfo2": data_payload}))
    
    xml_similar = ""
    for s in final_similar:
        xml_similar += f'<similarArtist id="{s.get("id")}" name="{html.escape(s.get("name", "Unknown"))}" coverArt="{s.get("coverArt") or ""}" albumCount="0" />'
    
    xml_content = f'''<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><artistInfo><biography>{html.escape(final_bio)}</biography><largeImageUrl>{final_image}</largeImageUrl><mediumImageUrl>{final_image}</mediumImageUrl><smallImageUrl>{final_image}</smallImageUrl>{xml_similar}</artistInfo></subsonic-response>'''
    return Response(content=xml_content, media_type="application/xml")

@router.get("/getAlbumList2")
@router.get("/getAlbumList2.view")
async def subsonic_get_album_list(
    u: str = Query(None),
    db: Session = Depends(get_db),
    type: str = "newest", size: int = 50, offset: int = 0, f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    search_query = "Top" 
    if type == "random": search_query = "Mix"
    elif type == "alphabeticalByName": search_query = "A" 
    elif type == "recent": search_query = "New"
    elif type == "newest": search_query = "New"
    elif type == "frequent": search_query = "Pop"
    elif type == "starred": search_query = "Best"

    if not tidal_token or not global_vars.shared_api_client:
        if f == "json": return JSONResponse(content=create_subsonic_response({"albumList2": {"album": []}}))
        else: return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><albumList2></albumList2></subsonic-response>', media_type="application/xml")

    headers = {"authorization": f"Bearer {tidal_token}"}
    url = f"https://api.tidal.com/v1/search/albums?query={search_query}&limit={size}&offset={offset}&countryCode={REGION}"
    
    try:
        res = await get_tidal_request(url, headers)
        items = []
        if res and res.status_code == 200:
            data = res.json()
            items = data.get("albums", {}).get("items", [])
            if not items and isinstance(data.get("items"), list):
                    items = data.get("items")
        
        albums = []
        for item in items:
            if not item.get("id"): continue
            safe_id = safe_xml_id(item.get("id"))
            artists_list = item.get("artists", [])
            artist_name = "Unknown"
            artist_id = "0"
            if artists_list and len(artists_list) > 0:
                artist_name = f"(T) {artists_list[0].get('name', 'Unknown')}"
                artist_id = safe_xml_id(artists_list[0].get("id"))
            alb_title = f"(T) {item.get('title', 'Unknown Album')}"
            cov_uuid = item.get("cover")
            cov_val = f"tidal_{cov_uuid}" if cov_uuid else f"tidal_{safe_id}"
            
            albums.append({
                "id": f"tidal_{safe_id}", "parent": "1", "title": alb_title, "name": alb_title, 
                "artist": artist_name, "artistId": f"tidal_{artist_id}", "isDir": True,
                "coverArt": cov_val, 
                "created": item.get("releaseDate", "2000-01-01"),
                "year": int(item.get("releaseDate", "2000")[:4]) if item.get("releaseDate") else 2000
            })

        if f == "json": return JSONResponse(content=create_subsonic_response({"albumList2": {"album": albums}}))
        
        xml_items = ""
        for alb in albums:
            s_id = safe_xml_id(alb["id"])
            s_artist_id = safe_xml_id(alb["artistId"])
            s_title = html.escape(str(alb["title"]))
            s_artist = html.escape(str(alb["artist"]))
            xml_items += f'<album id="{s_id}" parent="1" name="{s_title}" title="{s_title}" artist="{s_artist}" artistId="{s_artist_id}" isDir="true" coverArt="{alb["coverArt"]}" created="{alb["created"]}" year="{alb["year"]}"/>'
        
        xml_content = f'''<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><albumList2>{xml_items}</albumList2></subsonic-response>'''
        return Response(content=xml_content, media_type="application/xml")
    except Exception:
         if f == "json": return JSONResponse(content=create_subsonic_response({"albumList2": {"album": []}}))
         return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><albumList2></albumList2></subsonic-response>', media_type="application/xml")

@router.get("/getAlbum")
@router.get("/getAlbum.view")
async def subsonic_get_album(
    id: str, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    clean_id = id.replace("tidal_", "")
    headers = {"authorization": f"Bearer {tidal_token}"} if tidal_token else {}
    
    try:
        album_url = f"https://api.tidal.com/v1/albums/{clean_id}?countryCode={REGION}"
        tracks_url = f"https://api.tidal.com/v1/albums/{clean_id}/items?countryCode={REGION}&limit=100"
        
        if not global_vars.shared_api_client: raise Exception("No Client")

        album_res, tracks_res = await asyncio.gather(
            get_tidal_request(album_url, headers),
            get_tidal_request(tracks_url, headers)
        )
            
        if not album_res or album_res.status_code != 200: raise Exception("Album not found")
        album_data = album_res.json()
        tracks_data = tracks_res.json() if tracks_res and tracks_res.status_code == 200 else {}
        alb_cover_uuid = album_data.get("cover")
        alb_cover_val = f"tidal_{alb_cover_uuid}" if alb_cover_uuid else f"tidal_{album_data.get('id')}"
        album_artist_name = f"(T) {album_data.get('artist', {}).get('name')}"
        album_artist_id = f"tidal_{album_data.get('artist', {}).get('id')}"
        songs = []
        for item in tracks_data.get("items", []):
            track = item.get("item") or item
            songs.append({
                "id": f"tidal_{track.get('id')}", 
                "parent": id, 
                "title": track.get("title"),
                "artist": f"(T) {track.get('artist', {}).get('name')}", 
                "artistId": f"tidal_{track.get('artist', {}).get('id')}",
                "album": f"(T) {track.get('album', {}).get('title')}", 
                "albumId": f"tidal_{track.get('album', {}).get('id')}",
                "albumArtist": album_artist_name,
                "albumArtistId": album_artist_id,
                "coverArt": alb_cover_val, 
                "duration": track.get("duration") or 0,
                "track": track.get("trackNumber") or 1, 
                "discNumber": track.get("volumeNumber") or 1,
                "year": int(track.get("streamStartDate", "0")[:4]) if track.get("streamStartDate") else 0,
                "isDir": False, "contentType": "audio/flac", "suffix": "flac", "bitRate": 1411, "size": 30000000,
                "path": f"Tidal/{album_data.get('title')}/{track.get('title')}.flac" 
            })
        
        if f == "json":
            return JSONResponse(content=create_subsonic_response({
                "album": {
                    "id": id, "name": f"(T) {album_data.get('title')}",
                    "artist": album_artist_name,
                    "artistId": album_artist_id,
                    "coverArt": alb_cover_val,
                    "song": songs, "songCount": len(songs),
                    "duration": sum(s["duration"] for s in songs) if songs else 0,
                    "created": album_data.get("releaseDate", "")
                }
            }))
        
        song_xml_str = ""
        for s in songs:
            song_xml_str += f'<song id="{s["id"]}" parent="{s["parent"]}" title="{escape_xml(s["title"])}" artist="{escape_xml(s["artist"])}" album="{escape_xml(s["album"])}" albumArtist="{escape_xml(s["albumArtist"])}" isDir="false" coverArt="{s["coverArt"]}" duration="{s["duration"]}" track="{s["track"]}" discNumber="{s["discNumber"]}" year="{s["year"]}" suffix="flac" contentType="audio/flac" bitRate="1411" size="30000000" path="{escape_xml(s["path"])}"/>'

        xml_content = f'''<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><album id="{id}" name="{escape_xml("(T) " + album_data.get("title"))}" title="{escape_xml("(T) " + album_data.get("title"))}" artist="{escape_xml(album_artist_name)}" artistId="{album_artist_id}" coverArt="{alb_cover_val}" songCount="{len(songs)}" duration="{sum(s["duration"] for s in songs) if songs else 0}" created="{album_data.get("releaseDate", "")}">{song_xml_str}</album></subsonic-response>'''
        return Response(content=xml_content, media_type="application/xml")

    except Exception:
        if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": "Album not found"}}))
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="70" message="Album not found"/></subsonic-response>', media_type="application/xml")

@router.get("/getSong")
@router.get("/getSong.view")
async def subsonic_get_song(
    id: str, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    clean_id = id.replace("tidal_", "")
    headers = {"authorization": f"Bearer {tidal_token}"}
    try:
        if not global_vars.shared_api_client: raise Exception("No Client")
        res = await get_tidal_request(f"https://api.tidal.com/v1/tracks/{clean_id}?countryCode={REGION}", headers)
        if not res or res.status_code != 200:
            if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": "Song not found"}}))
            return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="70" message="Song not found"/></subsonic-response>', media_type="application/xml")
        
        track = res.json()
        alb_uuid = track.get("album", {}).get("cover")
        alb_id = track.get("album", {}).get("id")
        cover_val = f"tidal_{alb_uuid}" if alb_uuid else f"tidal_{alb_id}"
        
        s = {
            "id": f"tidal_{track.get('id')}", "parent": f"tidal_{alb_id}",
            "title": track.get("title"), "artist": f"(T) {track.get('artist', {}).get('name')}",
            "album": f"(T) {track.get('album', {}).get('title')}", "coverArt": cover_val,
            "duration": track.get("duration"), "track": track.get("trackNumber"),
            "year": int(track.get("streamStartDate", "0")[:4]) if track.get("streamStartDate") else 0,
            "suffix": "flac", "contentType": "audio/flac", "bitRate": 1411, "size": 30000000 
        }

        if f == "json": return JSONResponse(content=create_subsonic_response({"song": s}))
        xml_song = f'<song id="{s["id"]}" parent="{s["parent"]}" title="{escape_xml(s["title"])}" artist="{escape_xml(s["artist"])}" album="{escape_xml(s["album"])}" isDir="false" coverArt="{s["coverArt"]}" duration="{s["duration"]}" track="{s["track"]}" year="{s["year"]}" suffix="flac" contentType="audio/flac" bitRate="1411" size="30000000"/>'
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><song>{xml_song}</song></subsonic-response>', media_type="application/xml")
    except Exception as e:
        return Response(status_code=500)

@router.get("/scrobble")
@router.get("/scrobble.view")
async def subsonic_scrobble(id: str, submission: bool = True, f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response())
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"/>', media_type="application/xml")

@router.get("/getLyrics")
@router.get("/getLyrics.view")
@router.get("/getLyricsBySongId")      
@router.get("/getLyricsBySongId.view") 
async def subsonic_get_lyrics(
    artist: str = None, 
    title: str = None, 
    id: str = None, 
    songId: str = None, 
    f: str = None, 
    c: str = None,
    u: str = Query(None),
    db: Session = Depends(get_db)
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)
    target_id_param = id or songId
    client_id = c or "Unknown"
    lyrics_text = ""
    target_tidal_id = None
    if target_id_param and "tidal_" in target_id_param:
        target_tidal_id = target_id_param.replace("tidal_", "")
    elif title and global_vars.shared_api_client:
        try:
            search_q = str(title)
            if artist and artist.lower() != "unknown": 
                search_q += f" {artist}"
            
            headers = {"authorization": f"Bearer {tidal_token}"}
            search_url = f"https://api.tidal.com/v1/search/tracks?query={quote(search_q)}&limit=1&countryCode={REGION}"
            s_res = await get_tidal_request(search_url, headers)
            if s_res and s_res.status_code == 200:
                items = s_res.json().get("items", [])
                if items:
                    target_tidal_id = str(items[0].get("id"))
        except Exception:
            pass

    if target_tidal_id and global_vars.shared_api_client:
        headers = {"authorization": f"Bearer {tidal_token}"}
        try:
            url = f"https://api.tidal.com/v1/tracks/{target_tidal_id}/lyrics?countryCode={REGION}&locale=en_US&deviceType=BROWSER"
            res = await get_tidal_request(url, headers)
            
            if res and res.status_code == 200:
                data = res.json()
                raw_lrc = data.get("subtitles")
                raw_txt = data.get("lyrics")
                
                lyrics_text = raw_lrc if raw_lrc else raw_txt

                if lyrics_text:
                    lyrics_text = lyrics_text.replace("\r\n", "\n").replace("\r", "\n")
                                
        except Exception as e:
            pass

    if not lyrics_text:
        if f == "json": 
            return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": "Lyrics not found"}}))
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="70" message="Lyrics not found"/></subsonic-response>', media_type="application/xml")

    final_id = target_id_param if target_id_param else (f"tidal_{target_tidal_id}" if target_tidal_id else "0")
    
    lyrics_entry = {
        "id": final_id,
        "artist": artist or "Unknown",
        "title": title or "Unknown",
        "value": lyrics_text 
    }

    if f == "json":
        return JSONResponse(content=create_subsonic_response({"lyrics": lyrics_entry}))
    
    xml_content = (
        f'<lyrics id="{escape_xml(lyrics_entry["id"])}" '
        f'artist="{escape_xml(lyrics_entry["artist"])}" '
        f'title="{escape_xml(lyrics_entry["title"])}">'
        f'{escape_xml(lyrics_entry["value"])}'
        f'</lyrics>'
    )
    
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1">{xml_content}</subsonic-response>', media_type="application/xml")

@router.get("/getSimilarSongs")
@router.get("/getSimilarSongs.view")
async def subsonic_get_similar_songs(id: str, count: int = 50, f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response({"similarSongs": {"song": []}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><similarSongs></similarSongs></subsonic-response>', media_type="application/xml")

@router.get("/getIndexes")
@router.get("/getIndexes.view")
async def subsonic_get_indexes(f: str = "json"):
     return JSONResponse(content=create_subsonic_response({
         "indexes": {
             "lastModified": int(time.time() * 1000),
             "index": [{"name": "Tidal", "artist": []}]
         }
     }))

@router.get("/getMusicDirectory.view")
async def subsonic_get_music_directory(
    id: str, 
    request: Request, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    f: str = None
):
    if id == "1":
        return await subsonic_get_album_list(f=f)
    
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    if id.startswith("nav_") or (id.isdigit() and "tidal_" not in id):
        clean_id = id.replace("nav_", "")
        clean_id = re.sub(r'_\d+$', '', clean_id)
        try:
            full_params = dict(request.query_params)
            full_params["id"] = clean_id
            
            if "t" not in full_params:
                auth = get_auth_params(settings)
                full_params.update(auth)

            base_url = settings.nav_url.rstrip('/') if settings and settings.nav_url else ""
            if base_url:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{base_url}/rest/getMusicDirectory.view", params=full_params)
                    if resp.status_code == 200:
                        content = resp.content.decode('utf-8')
                        if f == "json":
                            content = re.sub(r'"id"\s*:\s*"(\d+)"', r'"id":"nav_\1"', content)
                            content = re.sub(r'"coverArt"\s*:\s*"(\d+)"', r'"coverArt":"nav_\1"', content)
                            content = re.sub(r'"artistId"\s*:\s*"(\d+)"', r'"artistId":"nav_\1"', content)
                            content = re.sub(r'"albumId"\s*:\s*"(\d+)"', r'"albumId":"nav_\1"', content)
                            content = re.sub(r'"parent"\s*:\s*"(\d+)"', r'"parent":"nav_\1"', content)
                            return Response(content=content, media_type="application/json")
                        else:
                            content = re.sub(r'id="(\d+)"', r'id="nav_\1"', content)
                            content = re.sub(r'parent="(\d+)"', r'parent="nav_\1"', content)
                            content = re.sub(r'coverArt="(\d+)"', r'coverArt="nav_\1"', content)
                            content = re.sub(r'artistId="(\d+)"', r'artistId="nav_\1"', content)
                            content = re.sub(r'albumId="(\d+)"', r'albumId="nav_\1"', content)
                            return Response(content=content, media_type="text/xml")
        except Exception:
            pass

    if not tidal_token:
        error_msg = "Tidal token missing"
        if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": error_msg}}))
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="70" message="{error_msg}"/></subsonic-response>', media_type="application/xml")

    clean_id = id.replace("tidal_", "")
    headers = {"authorization": f"Bearer {tidal_token}"}

    try:
        res_alb = await get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}?countryCode={REGION}", headers)
        if res_alb and res_alb.status_code == 200:
            alb_data = res_alb.json()
            alb_name = alb_data.get("title", "Unknown")
            
            res_tracks = await get_tidal_request(f"https://api.tidal.com/v1/albums/{clean_id}/items?countryCode={REGION}&limit=100", headers)
            tracks_data = []
            if res_tracks and res_tracks.status_code == 200:
                tracks_data = res_tracks.json().get("items", [])
            children = []
            alb_cover_uuid = alb_data.get("cover")
            alb_cover_val = f"tidal_{alb_cover_uuid}" if alb_cover_uuid else f"tidal_{alb_data.get('id')}"

            for item in tracks_data:
                track = item.get("item") or item
                if not track: continue
                
                track_num = track.get("trackNumber") or 1
                disc_num = track.get("volumeNumber") or 1
                year = int(track.get("streamStartDate", "2000")[:4]) if track.get("streamStartDate") else 2000
                duration = track.get("duration") or 0
                
                child = {
                    "id": f"tidal_{track.get('id')}",
                    "parent": id,
                    "title": track.get("title"),
                    "artist": f"(T) {track.get('artist', {}).get('name')}",
                    "album": f"(T) {alb_name}",
                    "isDir": False, 
                    "coverArt": alb_cover_val,
                    "duration": duration,
                    "track": track_num,
                    "discNumber": disc_num,
                    "year": year,
                    "size": 30000000,
                    "suffix": "flac",
                    "contentType": "audio/flac",
                    "path": f"Tidal/{alb_name}/{track.get('title')}.flac",
                    "isVideo": False
                }
                children.append(child)

            if f == "json":
                return JSONResponse(content=create_subsonic_response({
                    "directory": {"id": id, "name": f"(T) {alb_name}", "child": children}
                }))

            xml_children = ""
            for c in children:
                xml_children += (
                    f'<child id="{c["id"]}" parent="{c["parent"]}" title="{escape_xml(c["title"])}" '
                    f'artist="{escape_xml(c["artist"])}" album="{escape_xml(c["album"])}" isDir="false" '
                    f'coverArt="{c["coverArt"]}" duration="{c["duration"]}" track="{c["track"]}" '
                    f'discNumber="{c["discNumber"]}" year="{c["year"]}" size="{c["size"]}" '
                    f'suffix="flac" contentType="audio/flac" isVideo="false" path="{escape_xml(c["path"])}"/>'
                )
            
            return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><directory id="{id}" name="{escape_xml("(T) " + alb_name)}">{xml_children}</directory></subsonic-response>', media_type="application/xml")
    except Exception:
        pass

    try:
        t_art_alb = await get_tidal_request(f"https://api.tidal.com/v1/artists/{clean_id}/albums?countryCode={REGION}&limit=50", headers)
        if t_art_alb and t_art_alb.status_code == 200:
            items = t_art_alb.json().get("items", [])
            children = []
            for alb in items:
                cov = alb.get("cover")
                cov_val = f"tidal_{cov}" if cov else f"tidal_{alb.get('id')}"
                
                children.append({
                    "id": f"tidal_{alb.get('id')}", "parent": id,
                    "title": f"(T) {alb.get('title')}", "artist": f"(T) {alb.get('artist', {}).get('name')}",
                    "isDir": True, "coverArt": cov_val,
                    "year": int(alb.get("releaseDate", "2000")[:4]) if alb.get("releaseDate") else 2000
                })
                
            if f == "json":
                return JSONResponse(content=create_subsonic_response({"directory": {"id": id, "name": "Artist", "child": children}}))
            
            xml_children = ""
            for c in children:
                xml_children += f'<child id="{c["id"]}" parent="{id}" title="{escape_xml(c["title"])}" artist="{escape_xml(c["artist"])}" isDir="true" coverArt="{c["coverArt"]}" year="{c["year"]}"/>'
            return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><directory id="{id}" name="Artist">{xml_children}</directory></subsonic-response>', media_type="application/xml")
    except Exception:
        pass

    error_msg = "Directory not found"
    if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": error_msg}}))
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="70" message="{error_msg}"/></subsonic-response>', media_type="application/xml")
    
@router.get("/search3")
@router.get("/search3.view")
async def subsonic_search3(
    query: str, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    songCount: int = 20, albumCount: int = 20, artistCount: int = 20, f: str = None
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    if not tidal_token:
        if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 40, "message": "Tidal token not found"}}))
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="40" message="Tidal token not found"/></subsonic-response>', media_type="application/xml")
    
    headers = {"authorization": f"Bearer {tidal_token}"}
    limit = max(songCount, albumCount, artistCount)
    if limit > 50: limit = 50
    url = f"https://api.tidal.com/v1/search?query={quote(query)}&limit={limit}&offset=0&types=ARTISTS,ALBUMS,TRACKS&countryCode={REGION}"
    
    try:
        if not global_vars.shared_api_client: raise Exception("No Client")
        res = await get_tidal_request(url, headers)
        if not res: raise Exception("Search failed")
        data = res.json()
        sub_artists = []
        for item in data.get("artists", {}).get("items", []):
            pic = item.get("picture")
            sub_artists.append({
                "id": f"tidal_{item.get('id')}", "name": f"(T) {item.get('name')}",
                "coverArt": f"tidal_{pic}" if pic else "", "artistImageUrl": get_tidal_image_url(pic, 320)
            })
        sub_albums = []
        for item in data.get("albums", {}).get("items", []):
            title_suffix = ""
            modes = item.get("audioModes", [])
            if "DOLBY_ATMOS" in modes: title_suffix += " [Atmos]"
            elif "SONY_360RA" in modes: title_suffix += " [360]"
            elif item.get("audioQuality") == "HI_RES": title_suffix += " [Hi-Res]"
            alb_title = f"(T) {item.get('title')}{title_suffix}"
            
            cov = item.get("cover")
            cov_val = f"tidal_{cov}" if cov else f"tidal_{item.get('id')}"
            
            sub_albums.append({
                "id": f"tidal_{item.get('id')}", "parent": "1", "title": alb_title, "name": alb_title,
                "artist": f"(T) {item.get('artists')[0].get('name')}" if item.get("artists") else "Unknown",
                "artistId": f"tidal_{item.get('artists')[0].get('id')}" if item.get("artists") else "",
                "isDir": True, "coverArt": cov_val, "created": item.get("releaseDate", ""),
                "year": int(item.get("releaseDate", "0")[:4]) if item.get("releaseDate") else 0
            })
        sub_songs = []
        for item in data.get("tracks", {}).get("items", []):
            cov = item.get("album", {}).get("cover")
            cov_val = f"tidal_{cov}" if cov else f"tidal_{item.get('album', {}).get('id')}"
            sub_songs.append({
                "id": f"tidal_{item.get('id')}", "parent": f"tidal_{item.get('album', {}).get('id')}",
                "title": item.get("title"), "artist": f"(T) {item.get('artists')[0].get('name')}" if item.get("artists") else "Unknown",
                "artistId": f"tidal_{item.get('artists')[0].get('id')}" if item.get("artists") else "",
                "album": f"(T) {item.get('album', {}).get('title')}", "albumId": f"tidal_{item.get('album', {}).get('id')}",
                "coverArt": cov_val, "isDir": False,
                "duration": item.get("duration"), "track": item.get("trackNumber"), "contentType": "audio/flac", "size": 30000000, "suffix": "flac"
            })
        if f == "json":
            return JSONResponse(content=create_subsonic_response({
                "searchResult3": {
                    "artist": sub_artists[:artistCount], "album": sub_albums[:albumCount], "song": sub_songs[:songCount]
                }
            }))
        xml_body = ""
        for a in sub_artists[:artistCount]:
            xml_body += f'<artist id="{a["id"]}" name="{html.escape(a["name"])}" coverArt="{a["coverArt"] or ""}"/>'
        for a in sub_albums[:albumCount]:
            xml_body += f'<album id="{a["id"]}" parent="1" name="{html.escape(a["title"])}" title="{html.escape(a["title"])}" artist="{html.escape(a["artist"])}" isDir="true" coverArt="{a["coverArt"]}" year="{a["year"]}" created="{a["created"]}"/>'
        for s in sub_songs[:songCount]:
            xml_body += f'<song id="{s["id"]}" parent="{s["parent"]}" title="{html.escape(s["title"])}" artist="{html.escape(s["artist"])}" album="{html.escape(s["album"])}" isDir="false" coverArt="{s["coverArt"]}" duration="{s["duration"]}" track="{s["track"]}" suffix="flac" contentType="audio/flac"/>'
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><searchResult3>{xml_body}</searchResult3></subsonic-response>', media_type="application/xml")
    except Exception as e:
        if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 0, "message": str(e)}}))
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="0" message="{str(e)}"/></subsonic-response>', media_type="application/xml")

@router.get("/getTopSongs")
@router.get("/getTopSongs.view")
async def subsonic_get_top_songs(
    artist: str, 
    u: str = Query(None),
    db: Session = Depends(get_db),
    count: int = 50, f: str = "json"
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    headers = {"authorization": f"Bearer {tidal_token}"}
    artist_id = await search_tidal_id(artist, tidal_token, ref_albums=None)
    if not artist_id: return JSONResponse(content=create_subsonic_response({"topSongs": {"song": []}}))
    url = f"https://api.tidal.com/v1/artists/{artist_id}/toptracks?countryCode={REGION}&limit={count}"
    try:
        if not global_vars.shared_api_client: raise Exception("No Client")
        res = await get_tidal_request(url, headers)
        if not res or res.status_code != 200: return JSONResponse(content=create_subsonic_response({"topSongs": {"song": []}}))
        songs = []
        for item in res.json().get("items", []):
                cov = item.get("album", {}).get("cover")
                cov_val = f"tidal_{cov}" if cov else f"tidal_{item.get('album', {}).get('id')}"
                songs.append({
                    "id": f"tidal_{item.get('id')}", "parent": f"tidal_{item.get('album', {}).get('id')}", "title": item.get("title"), 
                    "artist": f"(T) {item.get('artists')[0].get('name')}", "album": f"(T) {item.get('album', {}).get('title')}", 
                    "coverArt": cov_val, "duration": item.get("duration"), 
                    "track": item.get("trackNumber"), "suffix": "flac", "contentType": "audio/flac", "bitRate": 1411, "size": 30000000, "isDir": False
                })
        return JSONResponse(content=create_subsonic_response({"topSongs": {"song": songs}}))
    except Exception as e:
         return JSONResponse(content=create_subsonic_response({"error": {"code": 0, "message": str(e)}}))

@router.get("/stream")
@router.get("/stream.view")
async def subsonic_stream(
    id: str, 
    request: Request, 
    u: str = Query(None), 
    t: str = None, s: str = None, f: str = None, 
    db: Session = Depends(get_db)
):
    if id.startswith("nav_"): return Response(status_code=404)
    
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    clean_id = id.replace("tidal_", "")
            
    if not tidal_token or not global_vars.shared_api_client: return Response(status_code=404)
    headers = {"authorization": f"Bearer {tidal_token}"}
    async def fetch_url(quality):
        url = f"https://api.tidal.com/v1/tracks/{clean_id}/playbackinfopostpaywall?audioquality={quality}&playbackmode=STREAM&assetpresentation=FULL"
        res = await get_tidal_request(url, headers)
        if res and res.status_code == 200:
            data = res.json()
            manifest_b64 = data.get("manifest")
            if manifest_b64:
                try:
                    manifest = json.loads(base64.b64decode(manifest_b64))
                    if manifest.get("urls"): return manifest["urls"][0]
                except: pass
        return None
    target_stream_url = await fetch_url("LOSSLESS")
    if not target_stream_url: target_stream_url = await fetch_url("HIGH")
    if not target_stream_url: return Response(status_code=404)
    return RedirectResponse(url=target_stream_url, status_code=302)

@router.get("/getPlaylists")
@router.get("/getPlaylists.view")
async def subsonic_get_playlists(u: str = None, f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response({"playlists": {"playlist": []}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><playlists></playlists></subsonic-response>', media_type="application/xml")

@router.get("/getArtists")
@router.get("/getArtists.view")
async def subsonic_get_artists(f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response({"artists": {"index": []}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><artists ignoredArticles="The El La Los Las Le Les"></artists></subsonic-response>', media_type="application/xml")

@router.get("/getGenres")
@router.get("/getGenres.view")
async def subsonic_get_genres(f: str = None):
    genres = [{"name": "Pop", "songCount": 100}, {"name": "Rock", "songCount": 100}, {"name": "Hip-Hop", "songCount": 100}]
    if f == "json": return JSONResponse(content=create_subsonic_response({"genres": {"genre": genres}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><genres><genre name="Pop" songCount="100"/></genres></subsonic-response>', media_type="application/xml")

@router.get("/getInternetRadioStations")
@router.get("/getInternetRadioStations.view")
async def subsonic_get_radio(f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response({"internetRadioStations": {"internetRadioStation": []}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><internetRadioStations></internetRadioStations></subsonic-response>', media_type="application/xml")

@router.get("/getScanStatus")
@router.get("/getScanStatus.view")
async def subsonic_get_scan_status(f: str = None):
    if f == "json": return JSONResponse(content=create_subsonic_response({"scanStatus": {"scanning": False, "count": 99999999}}))
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><scanStatus scanning="false" count="99999999"/></subsonic-response>', media_type="application/xml")

@router.get("/getRandomSongs")
@router.get("/getRandomSongs.view")
async def subsonic_get_random_songs(
    size: int = 20, 
    f: str = None, 
    u: str = Query(None),
    db: Session = Depends(get_db)
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    songs = []
    if tidal_token and global_vars.shared_api_client:
        headers = {"authorization": f"Bearer {tidal_token}"}
        url = f"https://api.tidal.com/v1/search/tracks?query=Mix&limit={size}&countryCode={REGION}"
        try:
            res = await get_tidal_request(url, headers)
            if res and res.status_code == 200:
                for item in res.json().get("items", []):
                    cov = item.get("album", {}).get("cover")
                    cov_val = f"tidal_{cov}" if cov else f"tidal_{item.get('album', {}).get('id')}"
                    songs.append({
                        "id": f"tidal_{item.get('id')}", "parent": f"tidal_{item.get('album', {}).get('id')}", "title": item.get("title"), 
                        "artist": item.get("artists")[0].get("name"), "album": f"(T) {item.get('album', {}).get('title')}", 
                        "coverArt": cov_val, "duration": item.get("duration"), 
                        "track": item.get("trackNumber"), "suffix": "flac", "contentType": "audio/flac", "bitRate": 1411, "size": 30000000, "isDir": False
                    })
        except: pass
    if f == "json": return JSONResponse(content=create_subsonic_response({"randomSongs": {"song": songs}}))
    xml_songs = ""
    for s in songs:
        xml_songs += f'<song id="{s["id"]}" parent="{s["parent"]}" title="{escape_xml(s["title"])}" artist="{escape_xml(s["artist"])}" album="{escape_xml(s["album"])}" coverArt="{s["coverArt"]}" duration="{s["duration"]}" track="{s["track"]}" suffix="flac" contentType="audio/flac" bitRate="1411" size="30000000"/>'
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><randomSongs>{xml_songs}</randomSongs></subsonic-response>', media_type="application/xml")
    
@router.get("/getPlaylist")
@router.get("/getPlaylist.view")
async def subsonic_get_playlist(
    id: str, 
    f: str = None, 
    u: str = Query(None),
    db: Session = Depends(get_db)
):
    settings = get_settings_by_username(u, db)
    tidal_token = get_user_tidal_token(settings)

    if not tidal_token:
         if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 40, "message": "Token not found"}}))
         return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="40" message="Token not found"/></subsonic-response>', media_type="application/xml")
    headers = {"authorization": f"Bearer {tidal_token}"}
    try:
        clean_id = id.replace("tidal_", "")
        if not global_vars.shared_api_client: raise Exception("No Client")
        pl_task = get_tidal_request(f"https://api.tidal.com/v1/playlists/{clean_id}?countryCode={REGION}", headers)
        items_task = get_tidal_request(f"https://api.tidal.com/v1/playlists/{clean_id}/items?countryCode={REGION}&limit=100", headers)
        pl_res, items_res = await asyncio.gather(pl_task, items_task, return_exceptions=True)
        if not pl_res or isinstance(pl_res, Exception) or pl_res.status_code != 200:
                if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 70, "message": "Playlist not found"}}))
                return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="70" message="Playlist not found"/></subsonic-response>', media_type="application/xml")
        pl_data = pl_res.json()
        items_data = items_res.json() if (items_res and not isinstance(items_res, Exception) and items_res.status_code == 200) else {}
        entries = []
        for item in items_data.get("items", []):
            track = item.get("item")
            if not track or track.get("type") != "TRACK": continue
            cov = track.get("album", {}).get("cover")
            cov_val = f"tidal_{cov}" if cov else f"tidal_{track.get('album', {}).get('id')}"
            entries.append({
                "id": f"tidal_{track.get('id')}", "parent": f"tidal_{track.get('album', {}).get('id')}", "title": track.get("title"),
                "artist": track.get("artist", {}).get("name"), "album": f"(T) {track.get('album', {}).get('title')}",
                "coverArt": cov_val, "duration": track.get("duration"), "track": track.get("trackNumber"),
                "year": int(track.get("streamStartDate", "0")[:4]) if track.get("streamStartDate") else 0,
                "suffix": "flac", "contentType": "audio/flac", "bitRate": 1411, "size": 30000000, "isDir": False
            })
        if f == "json":
            return JSONResponse(content=create_subsonic_response({
                "playlist": {
                    "id": f"tidal_{pl_data.get('id')}", "name": f"(T) {pl_data.get('title')}", "comment": pl_data.get("description"),
                    "owner": "admin", "public": True, "songCount": len(entries), "duration": sum(e["duration"] for e in entries),
                    "created": pl_data.get("created"), "coverArt": f"tidal_{pl_data.get('image')}", "entry": entries
                }
            }))
        xml_entries = ""
        for e in entries:
            xml_entries += f'<entry id="{e["id"]}" parent="{e["parent"]}" title="{escape_xml(e["title"])}" artist="{escape_xml(e["artist"])}" album="{escape_xml(e["album"])}" coverArt="{e["coverArt"]}" duration="{e["duration"]}" track="{e["track"]}" year="{e["year"]}" suffix="flac" contentType="audio/flac" bitRate="1411" size="30000000"/>'
        xml_content = f'''<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><playlist id="tidal_{pl_data.get("id")}" name="{escape_xml("(T) " + pl_data.get("title"))}" comment="{escape_xml(pl_data.get("description"))}" owner="admin" public="true" songCount="{len(entries)}" duration="{sum(e["duration"] for e in entries)}" created="{pl_data.get("created")}" coverArt="tidal_{pl_data.get("image")}">{xml_entries}</playlist></subsonic-response>'''
        return Response(content=xml_content, media_type="application/xml")
    except Exception as e:
         if f == "json": return JSONResponse(content=create_subsonic_response({"error": {"code": 0, "message": str(e)}}))
         return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="0" message="{str(e)}"/></subsonic-response>', media_type="application/xml")
         
@router.get("/getOpenSubsonicExtensions")
@router.get("/getOpenSubsonicExtensions.view")
async def subsonic_get_extensions(f: str = None):
    if f == "json":
        return JSONResponse(content=create_subsonic_response({"openSubsonicExtensions": []}))
    xml = '<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"><openSubsonicExtensions></openSubsonicExtensions></subsonic-response>'
    return Response(content=xml, media_type="application/xml")