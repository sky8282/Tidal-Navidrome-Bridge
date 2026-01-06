import os
import json
import base64
import asyncio
import sys
import logging
import traceback
import time
import html
import hashlib
import binascii
import secrets
from contextlib import asynccontextmanager
from typing import Union, Dict, Any
from urllib.parse import urlencode, quote, urlparse
from datetime import datetime, timedelta
from database import get_db, User, UserSettings
from sqlalchemy.orm import Session

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Depends, Cookie, WebSocket, APIRouter, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from asyncio import gather
from pydantic import BaseModel

try:
    from tidal_details_service import get_tidal_artist_details, get_tidal_album_details, get_tidal_artist_works_page
except ImportError:
    pass

try:
    from tidal_atmos import refresh_atmos_token, load_atmos_token, get_atmos_manifest, ATMOS_CLIENT_ID, ATMOS_CLIENT_SECRET
    from tidal_download_service import TOKEN_FILE, ATMOS_TOKEN_FILE, get_download_prep_info_py
    from tidal_api_service import (
        get_item_info_service,
        get_album_tracks_service,
        get_track_info_service,
        get_lyrics_service,
        get_video_playback_info_service,
        get_module_paged_data_service,
        tidal_fetch
    )
except ImportError:
    TOKEN_FILE = "tidal-token.json"
    ATMOS_TOKEN_FILE = "atmos-token.json"
    ATMOS_CLIENT_ID = "placeholder"
    ATMOS_CLIENT_SECRET = "placeholder"
    
    def load_atmos_token(*args): return {}
    async def refresh_atmos_token(*args): return {}
    async def get_atmos_manifest(*args, **kwargs): return None
    async def get_download_prep_info_py(*args, **kwargs): 
        print("Warning: tidal_download_service not found.")
        return {}
    async def get_item_info_service(*args, **kwargs): return None
    async def get_album_tracks_service(*args, **kwargs): return None
    async def get_track_info_service(*args, **kwargs): return None
    async def get_lyrics_service(*args, **kwargs): return None
    async def get_video_playback_info_service(*args, **kwargs): return None
    async def get_module_paged_data_service(*args, **kwargs): return None
    async def tidal_fetch(*args, **kwargs): return None


TIDAL_SEMAPHORE = asyncio.Semaphore(5)
shared_api_client = None

async def get_tidal_request(url: str, headers: dict, retries=3):
    if shared_api_client is None:
        async with httpx.AsyncClient() as temp_client:
            return await temp_client.get(url, headers=headers)
            
    for i in range(retries):
        try:
            async with TIDAL_SEMAPHORE:
                resp = await shared_api_client.get(url, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(1 + i)
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(0.5)
                    continue
                return resp
        except Exception as e:
            if i == retries - 1:
                print(f"Request failed: {url} - {e}")
                return None
            await asyncio.sleep(0.5)
    return None

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID", "6BDSRdpK9hqEBTgU")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "vRAdA108tlvkJpTsGZS8rGZ7xTlbJ0qaZ2K9saEzsgY=")
USER_ID = os.getenv("USER_ID")
REGION = os.getenv("REGION", "US")
API_KEY = os.getenv("API_KEY", "C9E1D6B0A7F8C2E4D9B1A0C7E3F6D8B2A4C9E1F0D7B3A6C8E2F0D1B4A5C9E1D6")
API_KEY_HEADER = "X-External-Api-Key"
CONFIG_FILE = "config.txt"
SECRET_KEY = "f5b8a3c9e1d6b0a7f8c2e4d9b1a0c7e3f6d8b2a4c9e1f0d7b3a6c8e2f0d1b4a5"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7 

class LoginRequest(BaseModel):
    username: str
    password: str

class PrepRequest(BaseModel):
    track_id: str | int | None = None
    album_id: str | int | None = None
    video_id: str | int | None = None
    quality: str = "LOSSLESS"
    region: str | None = None
    am_region: str | None = None
    settings: dict[str, Any] = {}

class NavidromeSettingsRequest(BaseModel):
    nav_url: str
    nav_username: str | None = None
    nav_password: str | None = None

def load_config():
    credentials = {}
    paths = [CONFIG_FILE, f"routers/{CONFIG_FILE}", f"../{CONFIG_FILE}"]
    found_path = None
    for p in paths:
        if os.path.exists(p):
            found_path = p
            break
    if not found_path: return credentials
    try:
        with open(found_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                if ":" in line:
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        credentials[parts[0].strip()] = parts[1].strip()
        return credentials
    except: return {}

def create_default_config_if_not_exists():
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f:
                f.write("admin:password\n")
        except: pass

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request, access_token: Union[str, None] = Cookie(default=None)):
    if not AUTH_ENABLED: return "public_user"
    token = access_token
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "): token = auth_header.split(" ")[1]
    if not token:
        nd_header = request.headers.get("x-nd-authorization")
        if nd_header and nd_header.startswith("Bearer "): token = nd_header.split(" ")[1]
    if not token: raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=403, detail="Invalid token")
        return username
    except JWTError: raise HTTPException(status_code=403, detail="Invalid token")

async def get_current_user_settings(
    current_user: str = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> UserSettings:
    user = db.query(User).filter(User.username == current_user).first()
    if user and user.settings:
        return user.settings
    return UserSettings() 

async def verify_api_key(request: Request):
    public_endpoints = [
        "/docs", "/openapi.json", "/check-auth", "/get-token-region", 
        "/login", "/api/v1/download/prep", "/rest", "/auth", "/logout", "/settings"
    ]
    if any(request.url.path.startswith(e) for e in public_endpoints): return True
    incoming_key = request.headers.get(API_KEY_HEADER)
    if incoming_key is None or incoming_key != API_KEY:
        raise HTTPException(status_code=403, detail=f"缺少或错误的 {API_KEY_HEADER}。")
    return True

def is_external_proxy_target(proxy_target: str) -> bool:
    if not proxy_target: return False
    try:
        parsed_url = urlparse(proxy_target)
        if parsed_url.hostname in ['localhost', '127.0.0.1', '0.0.0.0', None]: return False
        return True
    except: return True

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_default_config_if_not_exists()
    yield
    print("--- 服务已关闭 ---")

async def get_required_token(request: Request, default_key: str = 'base_info'):
    return None 

async def proxy_to_hifi(target_url: str) -> Any:
    async with httpx.AsyncClient() as client:
        res = await client.get(target_url)
        return res.json()

async def verify_subsonic_request(request: Request):
    return True

def get_subsonic_tidal_token():
    return None

def create_subsonic_response(data: dict = None, version: str = "1.16.1"):
    response = {
        "subsonic-response": {
            "status": "ok",
            "version": version,
            "type": "Tidal & Navidrome",
            "serverVersion": "https://github.com/sky8282/Tidal-Navidrome-Bridge",
            "openSubsonic": True
        }
    }
    if data: response["subsonic-response"].update(data)
    return response

def escape_xml(s: str) -> str:
    if not s: return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))

def get_db_nav_auth_params(settings: Any) -> Dict[str, str]:
    if not settings:
        return {}
    
    username = settings.nav_username 
    if not username: return {}

    password = settings.nav_password or ""
    
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode('utf-8')).hexdigest()
    
    return {
        "u": username,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "TidalProxy",
        "f": "json"
    }

def inject_subsonic_auth(params: dict) -> dict:
    return params
