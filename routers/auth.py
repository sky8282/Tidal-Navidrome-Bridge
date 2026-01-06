from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, Response, Cookie
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import timedelta
import os
import sys
import httpx
import asyncio
import json
import secrets
from jose import jwt
from database import get_db, User, UserSettings
from .globals import (
    LoginRequest, NavidromeSettingsRequest,
    get_current_user, 
    create_access_token, 
    ACCESS_TOKEN_EXPIRE_DAYS, ALGORITHM, 
    AUTH_ENABLED, TOKEN_FILE,
    verify_api_key,
    SECRET_KEY 
)

router = APIRouter()

@router.post("/api/testpost")
async def test_post_route():
    return {"message": "POST request received!"}

@router.websocket("/ws/run-login")
async def websocket_run_login(websocket: WebSocket):
    await websocket.accept()
    script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "login.py")
    current_user = "admin"
    token = websocket.cookies.get("access_token")
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_in_token = payload.get("sub")
            if user_in_token:
                current_user = user_in_token
        except Exception as e:
            print(f"WS Token Decode Error: {e}")
            pass
            
    print(f"--- [WS Login] 正在为用户 [{current_user}] 启动授权脚本 ---")
    cmd_args = [sys.executable, "-u", script_path, "--user", current_user]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        async def read_stream(stream, stream_name):
            while True:
                line = await stream.readline()
                if line:
                    line_text = line.decode('utf-8', errors='ignore').strip()
                    if line_text:
                        await websocket.send_text(f"[{stream_name}] {line_text}")
                else:
                    break
        await asyncio.gather(
            read_stream(process.stdout, "stdout"),
            read_stream(process.stderr, "stderr")
        )
        await process.wait()
    except Exception as e:
        print(f"WS Execution Error: {e}")
        try:
            await websocket.send_text(f"❌ Error: {str(e)}")
        except:
            pass
    finally:
        if process and process.returncode is None:
            try:
                process.terminate()
                await process.wait()
            except Exception:
                pass
        try:
            await websocket.close()
        except:
            pass

@router.get("/check-auth")
async def check_auth(current_user: str = Depends(get_current_user)):
    return {"username": current_user}

@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=403, detail="Login disabled")
    username = None
    password = None
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
    except Exception:
        try:
            form = await request.form()
            username = form.get("username")
            password = form.get("password")
        except Exception:
            pass
    if not username or not password:
        raise HTTPException(status_code=422, detail="Missing username or password")
    
    user = db.query(User).filter(User.username == username).first()
    if not user:
         user = db.query(User).filter(User.username == "admin").first()
         if username == "admin" and not user:
             pass 
    valid_login = False
    if user:
         if user.hashed_password == password: 
             valid_login = True
    else:
         if username == "admin" and password == "password":
             valid_login = True
    if valid_login:
        access_token_expires = timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
        access_token = create_access_token(
            data={"sub": username}, expires_delta=access_token_expires
        )
        response_content = {
            "id": "admin",
            "username": username,
            "name": username,
            "token": access_token,
            "isAdmin": True,
            "roles": {
                "admin": True, "stream": True, "jukebox": True, "download": True, 
                "upload": True, "coverArt": True, "settings": True, "podcast": True, 
                "share": True, "comment": True
            },
            "lastLoginAt": "2023-01-01T00:00:00.000000000Z",
            "message": "Login successful"
        }
        response = JSONResponse(
            content=response_content,
            headers={"x-nat-token": access_token} 
        )
        response.set_cookie(
            key="access_token", 
            value=access_token, 
            httponly=True,
            samesite="lax", 
            secure=False,
            path="/",
            max_age=int(access_token_expires.total_seconds()) 
        )
        return response
    else:
        raise HTTPException(status_code=401, detail="Invalid username or password")

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key="access_token", path="/", samesite="lax", secure=False, httponly=True)
    response.delete_cookie(key="session", path="/", samesite="lax", secure=False, httponly=True)
    return {"message": "Logged out"}

@router.get("/settings/navidrome")
async def get_navidrome_settings(current_user: str = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == current_user).first()
    if not user:
        user = db.query(User).filter(User.username == "admin").first()
    if user and user.settings:
        return {
            "nav_url": user.settings.nav_url,
            "nav_username": user.settings.nav_username,
            "nav_password": user.settings.nav_password
        }
    return {}

@router.post("/settings/navidrome")
async def save_navidrome_settings(
    settings: NavidromeSettingsRequest,
    current_user: str = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    target_username = settings.nav_username
    if not target_username:
        raise HTTPException(status_code=400, detail="Must provide navidrome username")
    user = db.query(User).filter(User.username == target_username).first()
    if not user:
        dummy_password = secrets.token_hex(8) 
        user = User(username=target_username, hashed_password=dummy_password)
        db.add(user)
        db.commit()
        db.refresh(user)
    if not user.settings:
        new_settings = UserSettings(user_id=user.id)
        db.add(new_settings)
        user.settings = new_settings
    clean_url = settings.nav_url.rstrip('/') if settings.nav_url else ""
    user.settings.nav_url = clean_url
    user.settings.nav_username = target_username
    user.settings.nav_password = settings.nav_password
    db.commit()
    return {"message": f"Configuration saved for user {target_username}"}

@router.get("/get-token-region")
async def get_token_region(
    current_user: str = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == current_user).first()
    if user and user.settings:
        settings = user.settings
        if settings.tidal_access_token:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        "https://api.tidal.com/v1/users/me",
                        headers={"Authorization": f"Bearer {settings.tidal_access_token}"}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        real_country = data.get("countryCode")
                        if real_country and real_country != settings.tidal_country_code:
                            print(f"--- [自动修正] 检测到地区不一致，正在更新: {settings.tidal_country_code} -> {real_country} ---")
                            settings.tidal_country_code = real_country
                            db.commit()
                            return {"region": real_country}
            except Exception as e:
                print(f"--- [自动修正] 获取 Tidal 用户信息失败: {e} ---")
        if settings.tidal_country_code:
             return {"region": settings.tidal_country_code}
    token_filepath = os.path.join(os.path.dirname(os.path.dirname(__file__)), TOKEN_FILE)
    if os.path.exists(token_filepath):
        try:
            with open(token_filepath, "r") as f:
                token_data = json.load(f)
            country_code = token_data.get("country_code")
            if country_code:
                return {"region": country_code}
        except:
            pass
    return {"region": None, "error": "Not bound"}

@router.post("/tidal/auth/refresh")
async def refresh_tidal_auth(
    current_user: str = Depends(get_current_user)
):
    script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "login.py")
    cmd_args = [sys.executable, "-u", script_path, "--refresh", "--user", current_user]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()
        if process.returncode == 0:
            return {"status": "ok", "message": stdout_str}
        else:
            print(f"[Auth Refresh Error] {stdout_str} {stderr_str}")
            raise HTTPException(status_code=401, detail=f"刷新失败: {stdout_str}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调用刷新脚本失败: {str(e)}")