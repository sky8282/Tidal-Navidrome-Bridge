import uvicorn
import os
import sys
import subprocess
import httpx
import asyncio
import urllib.parse
import itertools
import html
import hashlib
import secrets
import traceback
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from database import get_db, User, UserSettings, SessionLocal
from routers.globals import TOKEN_FILE, shared_api_client, inject_subsonic_auth
import routers.globals as global_vars
from routers import auth, subsonic, navidrome
from routers.auth import get_current_user

try:
    from routers.auth import get_password_hash, verify_password
except ImportError:
    #print("⚠️ Warning: Could not import auth functions. Password sync might fail.")
    def get_password_hash(p): return p
    def verify_password(plain, hashed): return plain == hashed

from routers.subsonic import subsonic_ping, subsonic_get_license, subsonic_get_user, subsonic_get_scan_status, subsonic_get_extensions, subsonic_get_cover_art

LOCAL_PORT = 8000
CONCURRENT_LIMIT = 1
IP_USER_MAP = {}

login_daemon_process = None

limits = httpx.Limits(max_keepalive_connections=20, max_connections=CONCURRENT_LIMIT)
timeout = httpx.Timeout(20.0, connect=5.0)

nav_semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

CONFIG_SEPARATOR = "-------------"

config_lock = threading.Lock()

def get_config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.txt")

def parse_config_file():
    config_path = get_config_path()
    users = {}
    codes = []
    
    if not os.path.exists(config_path):
        return users, codes

    with config_lock:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

    parts = content.split(CONFIG_SEPARATOR)
    user_part = parts[0].strip()
    code_part = parts[1].strip() if len(parts) > 1 else ""

    for line in user_part.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        
        clean_line = line
        if "]" in clean_line:
            clean_line = clean_line.split("]")[-1].strip()

        parts = clean_line.split(":", 1)
        if len(parts) == 2:
            u = parts[0].strip()
            p = parts[1].strip()
            if u and p:
                users[u] = p
    
    for line in code_part.splitlines():
        c = line.strip()
        if c:
            codes.append(c)

    return users, codes

def save_config_file(users_dict, codes_list):
    config_path = get_config_path()
    lines = []
    for u, p in users_dict.items():
        lines.append(f"{u}:{p}")
    
    lines.append(CONFIG_SEPARATOR)
    
    for c in codes_list:
        lines.append(c)
    
    with config_lock:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

def init_users_from_config():
    db = SessionLocal()
    try:
        config_users, _ = parse_config_file()
        existing_users = db.query(User).all()
        for username, plain_password in config_users.items():
            user = db.query(User).filter(User.username == username).first()
            if not user:
                print(f"--- [Sync] Adding new user: {username} ---")
                hashed = get_password_hash(plain_password)
                new_user = User(username=username, hashed_password=hashed)
                db.add(new_user)
            else:
                if not verify_password(plain_password, user.hashed_password):
                    print(f"--- [Sync] Updating password for: {username} ---")
                    user.hashed_password = get_password_hash(plain_password)
        for db_user in existing_users:
            if db_user.username not in config_users:
                print(f"--- [Sync] Deleting user not in config: {db_user.username} ---")
                db.query(UserSettings).filter(UserSettings.user_id == db_user.id).delete()
                db.delete(db_user)
        db.commit()
    except Exception as e:
        print(f"--- [Sync] ❌ Error syncing users: {e} ---")
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_users_from_config()
    global_vars.shared_api_client = httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        timeout=httpx.Timeout(15.0, connect=5.0),
        http2=True,
        follow_redirects=False
    )
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.py")
    global login_daemon_process
    try:
        login_daemon_process = subprocess.Popen(
            [sys.executable, script_path, "--daemon"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"--- [Main] 已启动 Token 自动刷新守护进程 (PID: {login_daemon_process.pid}) ---")
    except Exception as e:
        print(f"--- [Main] ❌ 启动守护进程失败: {e} ---")
    yield
    print("--- [Main] 正在关闭服务... ---")
    if global_vars.shared_api_client:
        await global_vars.shared_api_client.aclose()
    if login_daemon_process:
        print(f"--- [Main] 正在终止守护进程 (PID: {login_daemon_process.pid})... ---")
        login_daemon_process.terminate()
        try:
            login_daemon_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            login_daemon_process.kill()
        print("--- [Main] 守护进程已退出 ---")

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def sync_config_middleware(request: Request, call_next):
    if request.url.path == "/login" and request.method == "POST":
        try:
            await asyncio.to_thread(init_users_from_config)
        except Exception as e:
            print(f"❌ Login Sync Error: {e}")
    response = await call_next(request)
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "x-total-count", "Content-Range", "content-range"]
)

@app.post("/register")
async def register(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        invite_code = data.get("invite_code")
        if not username or not password or not invite_code:
            return JSONResponse(status_code=400, content={"detail": "请填写所有字段"})
        with config_lock:
             config_path = get_config_path()
             if not os.path.exists(config_path):
                 users, codes = {}, []
             else:
                 with open(config_path, "r", encoding="utf-8") as f:
                    content = f.read()
                 parts = content.split(CONFIG_SEPARATOR)
                 user_part = parts[0].strip()
                 code_part = parts[1].strip() if len(parts) > 1 else ""
                 users = {}
                 for line in user_part.splitlines():
                     line = line.strip()
                     if not line or line.startswith("#") or ":" not in line: continue
                     clean_line = line.split("]")[-1].strip() if "]" in line else line
                     p_ = clean_line.split(":", 1)
                     if len(p_) == 2: users[p_[0].strip()] = p_[1].strip()
                 codes = [c.strip() for c in code_part.splitlines() if c.strip()]
             if username in users:
                 return JSONResponse(status_code=400, content={"detail": "该用户名已被注册"})

             if invite_code not in codes:
                 return JSONResponse(status_code=400, content={"detail": "无效的邀请码"})
             codes.remove(invite_code)
             users[username] = password
             lines = []
             for u, p in users.items():
                 lines.append(f"{u}:{p}")
             lines.append(CONFIG_SEPARATOR)
             for c in codes:
                 lines.append(c)
             with open(config_path, "w", encoding="utf-8") as f:
                 f.write("\n".join(lines))
        hashed = get_password_hash(password)
        new_user = User(username=username, hashed_password=hashed)
        db.add(new_user)
        db.commit()
        return {"message": "注册成功"}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"注册失败: {str(e)}"})

def get_nav_signed_url(endpoint, params, settings):
    if not settings or not settings.nav_url:
        return ""
    base_url = settings.nav_url.rstrip('/')
    user = settings.nav_username
    password = settings.nav_password
    clean_params = {}
    for k, v in params.items():
        if v is not None and k not in ['u', 't', 's', 'p', 'jwt']:
            clean_params[k] = str(v)
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode('utf-8')).hexdigest()
    clean_params['u'] = user
    clean_params['t'] = token
    clean_params['s'] = salt
    clean_params['v'] = "1.16.1"
    clean_params['c'] = "Feishin"
    query_string = urllib.parse.urlencode(clean_params)
    return f"{base_url}/rest/{endpoint}.view?{query_string}"

def get_settings_by_username(username: str, db: Session):
    if not username:
        return None
    user = db.query(User).filter(User.username == username).first()
    if user and user.settings:
        return user.settings
    admin = db.query(User).filter(User.username == "admin").first()
    if admin and admin.settings:
        return admin.settings
    return None

def process_nav_data(data):
    target_keys = [
        "id", "parent", "albumId", "artistId", "coverArt", 
        "artistImageUrl", "avatar", "smallImageUrl", "mediumImageUrl", "largeImageUrl"
    ]
    if isinstance(data, dict):
        for k, v in data.items():
            if k in target_keys and isinstance(v, (str, int)):
                s_v = str(v)
                if s_v and s_v != "0" and not s_v.startswith("nav_") and not s_v.startswith("tidal_"):
                    data[k] = f"nav_{v}"
            else:
                process_nav_data(v)
    elif isinstance(data, list):
        for item in data:
            process_nav_data(item)
    return data

def process_tidal_data(data):
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "artist" or k == "artistName":
                if v and "(T)" not in str(v) and "(Tidal)" not in str(v):
                    data[k] = f"(T) {v}"
            elif (k == "title" or k == "name") and v:
                s_v = str(v)
                if not s_v.startswith("tidal_") and not s_v.startswith("nav_") and "(T)" not in s_v:
                     pass
            elif k == "genre":
                if v:
                    if "(T)" not in str(v) and "Tidal" not in str(v):
                        data[k] = f"{v}"
                else:
                    data[k] = "Tidal"
            elif k == "id" and v == "1" and "name" in data and data["name"] == "Tidal":
                data[k] = "tidal_main"
            else:
                process_tidal_data(v)
    elif isinstance(data, list):
        for item in data:
            process_tidal_data(item)
    return data

def fix_subsonic_response_data(data_list):
    items = data_list if isinstance(data_list, list) else [data_list]
    for item in items:
        is_local = str(item.get('id', '')).startswith('nav_') or 'minYear' in item or 'embedArtPath' in item
        if is_local:
            if 'coverArt' not in item or not item['coverArt']:
                item['coverArt'] = item.get('id')
            if 'year' not in item and 'minYear' in item:
                item['year'] = item['minYear']
            elif 'year' not in item and 'date' in item:
                try:
                    item['year'] = int(str(item['date'])[:4])
                except:
                    pass
            if 'created' not in item and 'date' in item:
                item['created'] = item['date']
        is_tidal = str(item.get('id', '')).startswith('tidal_')
        if is_tidal:
            if 'id' in item:
                item['coverArt'] = item['id']
            if 'songCount' not in item:
                 item['songCount'] = 0
    return items if isinstance(data_list, list) else items[0]

async def fetch_nav_data(endpoint, params, request: Request, db: Session):
    username = params.get('u')
    settings = get_settings_by_username(username, db)
    if not settings or not settings.nav_url:
        return None
    real_nav_url = settings.nav_url.rstrip('/')
    req_params = inject_subsonic_auth(dict(params))
    req_params['f'] = 'json'
    if 'jwt' in req_params: del req_params['jwt']
    salt = secrets.token_hex(6)
    token = hashlib.md5((settings.nav_password + salt).encode('utf-8')).hexdigest()
    req_params['u'] = settings.nav_username
    req_params['t'] = token
    req_params['s'] = salt
    try:
        async with nav_semaphore:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(f"{real_nav_url}/rest/{endpoint}.view", params=req_params, timeout=15.0)
                if resp.status_code == 200: return resp.json()
    except: 
        pass
    return None

async def fetch_tidal_internal(endpoint, query_params, headers=None): 
    req_params = dict(query_params)
    req_params['f'] = 'json'
    actual_headers = headers.copy() if headers else {}
    actual_headers["x-tidal-proxy-internal"] = "true" 
    try:
        url = f"http://127.0.0.1:{LOCAL_PORT}/tidal_proxy/rest/{endpoint}.view"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, params=req_params, headers=actual_headers, timeout=15.0)
            if resp.status_code == 200: return resp.json()
    except: pass
    return None

def dict_to_xml_attrs(item):
    attrs = ""
    for k, v in item.items():
        if not isinstance(v, (dict, list)):
            val_str = str(v)
            if isinstance(v, bool): val_str = "true" if v else "false"
            attrs += f' {k}="{html.escape(val_str)}"'
    return attrs

def build_xml_recursive(data, tag_name="entry"):
    xml = ""
    if isinstance(data, list):
        for item in data:
            xml += build_xml_recursive(item, tag_name)
    elif isinstance(data, dict):
        attrs = dict_to_xml_attrs(data)
        children_xml = ""
        for k, v in data.items():
            if isinstance(v, list):
                child_tag = "entry"
                tag_map = {"song": "song", "album": "album", "artist": "artist", "playlist": "playlist", "genre": "genre", "user": "user"}
                if k in tag_map: child_tag = tag_map[k]
                children_xml += build_xml_recursive(v, child_tag)
        xml += f'<{tag_name}{attrs}>{children_xml}</{tag_name}>'
    return xml

def convert_json_to_xml_response(json_data, endpoint):
    try:
        sub_resp = json_data.get("subsonic-response", {})
        status = sub_resp.get("status", "ok")
        version = sub_resp.get("version", "1.16.1")
        content_xml = ""
        core_data = {k: v for k, v in sub_resp.items() if k not in ["status", "version", "type", "serverVersion", "openSubsonic", "error"]}
        for wrapper_tag, content in core_data.items():
            inner_xml = ""
            if isinstance(content, list): inner_xml = build_xml_recursive(content, "entry")
            elif isinstance(content, dict):
                wrapper_attrs = dict_to_xml_attrs(content)
                children = ""
                for k, v in content.items():
                    if isinstance(v, list):
                        child_tag = "entry"
                        tag_map = {"song": "song", "album": "album", "artist": "artist"}
                        if k in tag_map: child_tag = tag_map[k]
                        children += build_xml_recursive(v, child_tag)
                if not children and not wrapper_attrs and "id" in content: inner_xml = build_xml_recursive(content, wrapper_tag)
                else:
                     if wrapper_tag in ["searchResult3", "searchResult2"]:
                         for list_name, list_data in content.items():
                             tag_map = {"song": "song", "album": "album", "artist": "artist"}
                             inner_xml += build_xml_recursive(list_data, tag_map.get(list_name, "entry"))
                         content_xml += f'<{wrapper_tag}{wrapper_attrs}>{inner_xml}</{wrapper_tag}>'
                         continue
                     content_xml += f'<{wrapper_tag}{wrapper_attrs}>{children}</{wrapper_tag}>'
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="{status}" version="{version}">{content_xml}</subsonic-response>', media_type="application/xml")
    except Exception as e:
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><subsonic-response status="failed"><error code="0" message="XML Conversion Failed: {str(e)}"/></subsonic-response>', media_type="application/xml")

app.include_router(subsonic.router, prefix="/tidal_proxy")
app.include_router(auth.router)
app.include_router(auth.router, prefix="/auth")
app.include_router(navidrome.router)

@app.delete("/settings/navidrome")
async def delete_navidrome_settings(
    db: Session = Depends(get_db),
    current_username: str = Depends(get_current_user)
):
    try:
        user = db.query(User).filter(User.username == current_username).first()
        if not user:
             return JSONResponse(status_code=404, content={"message": "User not found"})
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            return {"message": "No settings to delete"}
        settings.nav_url = ""
        settings.nav_username = ""
        settings.nav_password = ""
        db.commit()
        return {"message": "Navidrome configuration cleared"}
    except Exception as e:
        print(f"❌ Delete Navidrome Settings Error: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": f"Server Error: {str(e)}"})

@app.delete("/settings/tidal")
async def delete_tidal_settings(
    db: Session = Depends(get_db),
    current_username: str = Depends(get_current_user)
):
    try:
        user = db.query(User).filter(User.username == current_username).first()
        if not user:
             return JSONResponse(status_code=404, content={"message": "User not found"})
        settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if not settings:
            return {"message": "No settings to delete"}
        settings.tidal_access_token = None
        settings.tidal_refresh_token = None
        settings.tidal_expiry_time = None
        settings.tidal_session_id = None
        settings.tidal_country_code = None
        db.commit()
        return {"message": "Tidal configuration cleared"}
    except Exception as e:
        print(f"❌ Delete Tidal Settings Error: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": f"Server Error: {str(e)}"})

@app.api_route("/rest/{endpoint}", methods=["GET", "POST"])
@app.api_route("/rest/{endpoint}.view", methods=["GET", "POST"])
async def gateway_router(endpoint: str, request: Request, db: Session = Depends(get_db)):
    if endpoint.endswith(".view"): endpoint = endpoint[:-5]
    params = dict(request.query_params)
    target_id = params.get("id", "")
    if request.method == "POST":
        try:
            form_data = await request.form()
            params.update(form_data)
        except: pass
    if 'u' in params: IP_USER_MAP[request.client.host] = params['u']
    if endpoint in ["ping", "getLicense", "getUser", "getScanStatus", "getOpenSubsonicExtensions"]:
        secure_params = inject_subsonic_auth(params.copy())
        if endpoint == "ping": return await subsonic_ping(**secure_params)
        if endpoint == "getLicense": return await subsonic_get_license(f=params.get('f'))
        if endpoint == "getUser": return await subsonic_get_user(u=params.get('u'), f=params.get('f'))
        if endpoint == "getScanStatus": return await subsonic_get_scan_status(f=params.get('f'))
        if endpoint == "getOpenSubsonicExtensions": return await subsonic_get_extensions(f=params.get('f'))
    clean_target_id = target_id
    for prefix in ["al-", "ar-", "song-"]:
        if clean_target_id.startswith(prefix):
            clean_target_id = clean_target_id[len(prefix):]
            break
    is_nav_id = clean_target_id.startswith("nav_")
    is_tidal_id = clean_target_id.startswith("tidal_") or (clean_target_id.isdigit() and not is_nav_id)
    settings = get_settings_by_username(params.get('u'), db)
    if endpoint == "getCoverArt" or endpoint == "getAvatar":
        should_redirect_to_nav = False
        if is_nav_id:
            should_redirect_to_nav = True
        elif clean_target_id.isdigit() and not clean_target_id.startswith("tidal_") and settings:
            should_redirect_to_nav = True
        if should_redirect_to_nav:
             real_id = clean_target_id.replace("nav_", "")
             nav_params = params.copy()
             nav_params['id'] = real_id
             if not nav_params.get('size'): nav_params['size'] = "1000"
             if settings:
                 redirect_url = get_nav_signed_url(endpoint, nav_params, settings)
                 return RedirectResponse(url=redirect_url, status_code=302)
             else:
                 return Response(status_code=404)
        else:
            return await subsonic_get_cover_art(
                id=target_id,  
                u=params.get('u'),
                db=db,
                size=params.get("size"),
                f=params.get('f'),
                request=request
            )
    if endpoint in ["stream", "scrobble"]:
        if is_nav_id or (not is_tidal_id and settings):
            real_id = clean_target_id.replace("nav_", "")
            nav_params = params.copy()
            nav_params['id'] = real_id
            if settings:
                redirect_url = get_nav_signed_url(endpoint, nav_params, settings)
                return RedirectResponse(url=redirect_url, status_code=302)
            else:
                 return Response(status_code=404)
        else:
            secure_params = inject_subsonic_auth(params.copy())
            if 'jwt' in secure_params: del secure_params['jwt']
            if "id" in secure_params:
                secure_params["id"] = secure_params["id"].replace("nav_", "")
            
            query_str = urllib.parse.urlencode(secure_params)
            return RedirectResponse(f"/tidal_proxy/rest/{endpoint}.view?{query_str}", status_code=302)
    if endpoint in ["getIndexes", "getMusicFolders", "getAlbumList", "getAlbumList2", "search3", "search2", "getArtists", "getPlaylists", "getGenres", "getRandomSongs"]:
        forward_headers = {}
        if request.headers.get("Authorization"): forward_headers["Authorization"] = request.headers.get("Authorization")
        nav_res, tidal_res = await asyncio.gather(
            fetch_nav_data(endpoint, params, request, db),
            fetch_tidal_internal(endpoint, params, headers=forward_headers)
        )
        outer_keys = ["searchResult3", "indexes", "musicFolders", "albumList", "albumList2", "artists", "playlists", "genres", "randomSongs"]
        inner_keys = ["index", "musicFolder", "album", "artist", "song", "playlist", "genre"]
        nav_payload = {}
        tidal_payload = {}
        wrapper = "albumList2"
        inner_tag = "album"
        if nav_res:
            try:
                sub = nav_res.get("subsonic-response", {})
                for key in outer_keys:
                    if key in sub:
                        data_content = sub[key]
                        wrapper = key
                        if isinstance(data_content, dict):
                            for inner_k in inner_keys:
                                if inner_k in data_content:
                                    inner_tag = inner_k
                                    nav_payload = data_content if endpoint.startswith("search") else data_content[inner_k]
                                    break
                        break
                nav_payload = process_nav_data(nav_payload)
            except: pass
        if tidal_res:
            try:
                sub = tidal_res.get("subsonic-response", {})
                for key in outer_keys:
                    if key in sub:
                        data_content = sub[key]
                        if isinstance(data_content, dict):
                            for inner_k in inner_keys:
                                if inner_k in data_content:
                                    tidal_payload = data_content if endpoint.startswith("search") else data_content[inner_k]
                                    break
                        break
                tidal_payload = process_tidal_data(tidal_payload)
            except: pass
        final_data = []
        if endpoint in ["search3", "search2"]:
            final_data = {"song": [], "album": [], "artist": []}
            if not isinstance(nav_payload, dict): nav_payload = {}
            if not isinstance(tidal_payload, dict): tidal_payload = {}
            for k in ["song", "album", "artist"]:
                l1 = nav_payload.get(k, []) or []
                l2 = tidal_payload.get(k, []) or []
                if not isinstance(l1, list): l1 = [l1]
                if not isinstance(l2, list): l2 = [l2]
                final_data[k] = l1 + l2
        else:
            l1 = nav_payload if isinstance(nav_payload, list) else ([nav_payload] if nav_payload else [])
            l2 = tidal_payload if isinstance(tidal_payload, list) else ([tidal_payload] if tidal_payload else [])
            final_data = []
            for n, t in itertools.zip_longest(l1, l2):
                if n: final_data.append(n)
                if t: final_data.append(t)
        final_data = fix_subsonic_response_data(final_data)
        full_json = {"subsonic-response": {"status": "ok", "version": "1.16.1", wrapper: final_data if endpoint.startswith("search") else {inner_tag: final_data}}}
        return JSONResponse(full_json) if params.get('f') == 'json' else convert_json_to_xml_response(full_json, endpoint)
    if endpoint in ["getAlbum", "getSong", "getTopSongs", "getPlaylist"]:
        if is_nav_id or (not is_tidal_id and settings):
            real_id = clean_target_id.replace("nav_", "")
            nav_params = params.copy()
            nav_params['id'] = real_id
            nav_data = await fetch_nav_data(endpoint, nav_params, request, db)
            if nav_data:
                processed_data = process_nav_data(nav_data)
                return JSONResponse(processed_data) if params.get('f') == 'json' else convert_json_to_xml_response(processed_data, endpoint)
            return Response(status_code=404)
        else:
            secure_params = inject_subsonic_auth(params.copy())
            if "id" in secure_params:
                secure_params["id"] = secure_params["id"].replace("tidal_", "")
            query_str = urllib.parse.urlencode(secure_params)
            return RedirectResponse(f"/tidal_proxy/rest/{endpoint}.view?{query_str}", status_code=302)
    secure_params = inject_subsonic_auth(params.copy())
    if target_id.startswith("nav_"):
            secure_params["id"] = target_id 
    query_str = urllib.parse.urlencode(secure_params)
    return RedirectResponse(f"/tidal_proxy/rest/{endpoint}.view?{query_str}", status_code=302)

@app.get("/api/keepalive/keepalive")
def api_keepalive():
    return {"status": "ok"}

@app.get("/api/genre")
def api_genre():
    return Response(content='[]', media_type="application/json", headers={"X-Total-Count": "0"})
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=LOCAL_PORT, reload=True, proxy_headers=True, forwarded_allow_ips='*')