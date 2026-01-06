#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import time
import requests
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import tidalapi

try:
    from database import SessionLocal, User, UserSettings
    DB_AVAILABLE = True
except ImportError:
    print("❌ 错误: 无法导入数据库模块 (database.py)，请确保环境正确。")
    sys.exit(1)

HIRES_CLIENT_ID = "fX2JxdmntZWK0ixT"
HIRES_CLIENT_SECRET = "1Nn9AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg="

ACTIVE_CLIENT_ID = HIRES_CLIENT_ID
ACTIVE_CLIENT_SECRET = HIRES_CLIENT_SECRET

def get_db_session():
    return SessionLocal()

def get_user_id_by_username(db, username):
    user = db.query(User).filter(User.username == username).first()
    if user:
        return user.id
    if username == "admin":
        print(f"⚠️ 用户 admin 不存在，正在自动创建...")
        new_user = User(username="admin", hashed_password="password")
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        return new_user.id
    return None

def save_session_db(user_id, session):
    db = get_db_session()
    try:
        user_settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        if not user_settings:
            user_settings = UserSettings(user_id=user_id)
            db.add(user_settings)
        user_settings.tidal_access_token = session.access_token
        user_settings.tidal_refresh_token = session.refresh_token
        user_settings.tidal_expiry_time = int(session.expiry_time.timestamp())
        user_settings.tidal_session_id = session.session_id
        user_settings.tidal_country_code = session.country_code
        db.commit()
        print(f"✅ [DB] 用户 (ID: {user_id}) 的 Token 已成功保存到数据库")
        return True
    except Exception as e:
        print(f"❌ [DB] 保存失败: {e}")
        return False
    finally:
        db.close()

def refresh_user_token_logic(user_settings, db):
    if not user_settings.tidal_refresh_token:
        return False, "Refresh Token 缺失"
    url = "https://auth.tidal.com/v1/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": user_settings.tidal_refresh_token,
        "client_id": ACTIVE_CLIENT_ID,
        "client_secret": ACTIVE_CLIENT_SECRET,
        "scope": "r_usr w_usr w_sub" 
    }
    try:
        resp = requests.post(url, data=data, timeout=20)
        
        if resp.status_code == 200:
            token_data = resp.json()
            user_settings.tidal_access_token = token_data.get("access_token")
            if token_data.get("refresh_token"):
                user_settings.tidal_refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 0)
            user_settings.tidal_expiry_time = int(time.time()) + expires_in
            db.commit()
            return True, f"刷新成功 (Exp: {user_settings.tidal_expiry_time})"
        elif resp.status_code in [400, 401]:
             return False, f"Token 已失效 (HTTP {resp.status_code})"
        else:
            return False, f"HTTP {resp.status_code} - {resp.text}"
    except Exception as e:
        return False, str(e)

def login_interactive(username):
    print(f"🚀 开始为用户 [{username}] 进行 Tidal 授权...")
    db = get_db_session()
    user_id = get_user_id_by_username(db, username)
    db.close()
    if not user_id:
        print(f"❌ 错误: 数据库中找不到用户 [{username}]")
        return
    session = tidalapi.Session()
    session.client_id = ACTIVE_CLIENT_ID
    session.client_secret = ACTIVE_CLIENT_SECRET
    try:
        login, future = session.login_oauth()
        url_link = login.verification_uri_complete
        if not url_link.startswith("http"):
            url_link = "https://" + url_link
        print(f"URL: {url_link}")
        print(f"用户代码: {login.user_code}")
        print(f"等待授权完成...")
        future.result()
        if session.check_login():
            save_session_db(user_id, session)
            print(f"🎉 授权成功！区域: {session.country_code}")
        else:
            print("❌ 授权验证失败")
    except Exception as e:
        print(f"❌ 登录过程发生异常: {e}")

def run_refresh_oneshot(username):
    print(f"🔄 正在强制刷新用户 [{username}] 的 Token...")
    
    db = get_db_session()
    try:
        user_id = get_user_id_by_username(db, username)
        if not user_id:
            print("❌ 用户不存在")
            sys.exit(1)
        user_settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        if not user_settings:
            print("❌ 该用户未绑定 Tidal")
            sys.exit(1)
            
        success, msg = refresh_user_token_logic(user_settings, db)
        if success:
            print(f"✅ {msg}")
            sys.exit(0)
        else:
            print(f"❌ {msg}")
            sys.exit(1)
            
    except Exception as e:
        print(f"❌ 运行时异常: {e}")
        sys.exit(1)
    finally:
        db.close()

def run_daemon():
    print("🛡️  Tidal Token 守护进程已启动")
    
    while True:
        db = get_db_session()
        try:
            settings_list = db.query(UserSettings).filter(UserSettings.tidal_refresh_token != None).all()
            now = int(time.time())
            for us in settings_list:
                expiry = us.tidal_expiry_time or 0
                user_label = f"User_ID_{us.user_id}"
                if expiry < (now + 3600):
                    print(f"⏰ [{datetime.now()}] 用户 {user_label} Token 即将过期/已过期 (Exp: {expiry}), 正在刷新...")
                    success, msg = refresh_user_token_logic(us, db)
                    if success:
                        print(f"   -> 刷新成功")
                    else:
                        print(f"   -> 刷新失败: {msg}")
                else:
                    pass
                    
        except Exception as e:
            print(f"⚠️ [Daemon] 循环检查发生异常: {e}")
        finally:
            db.close()
        time.sleep(1800)

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="Tidal Login & Token Manager")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon to auto-refresh tokens")
    parser.add_argument("--refresh", action="store_true", help="Perform a one-time force refresh")
    parser.add_argument("--user", type=str, default="admin", help="Specify username (default: admin)")
    args = parser.parse_args()
    if args.daemon:
        run_daemon()
    elif args.refresh:
        run_refresh_oneshot(args.user)
    else:
        login_interactive(args.user)