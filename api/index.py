import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware

# 環境変数の設定チェック
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(supabase_url, supabase_key)

app = FastAPI()

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://server-bbs.vercel.app", "http://localhost:3000", "http://127.0.0.1:8000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 新しい連投制限設定
RATE_LIMIT_COUNT = 5  # 投稿許容回数 (5回)
RATE_LIMIT_WINDOW_SECONDS = 10  # 制限時間枠（秒） (10秒)

class PostData(BaseModel):
    name: str = "匿名"
    body: str

def generate_public_id(length=7):
    """一意な公開IDを生成"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

async def ban_user(public_id: str):
    """
    指定されたpublic_idをBANリストに登録し、システム通知として投稿を行います。
    """
    ban_reason = "連投制限（10秒間に5回以上）による自動BAN"
    
    # 1. BANリストに登録
    try:
        supabase.table("ban_list").insert({
            "public_id": public_id,
            "reason": ban_reason
        }).execute()
        print(f"BAN success: public_id {public_id} banned.")
    except Exception as e:
        print(f"BAN list insertion error (could be duplicate): {e}")

    # 2. ゆずbotとしてBAN通知を投稿
    notification_post = {
        "public_id": "system", # システム用の固定ID
        "name": "システム",
        "body": f"ID: {public_id} を連投制限超過のためBANしました。",
        "client_ip": "0.0.0.0",
        "created_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    
    try:
        supabase.table("posts").insert(notification_post).execute()
    except Exception as e:
        print(f"yuzu-bot notification post error: {e}")


@app.post("/post")
async def create_post(post: PostData, request: Request):
    
    # IPアドレスの取得
    client_ip = request.headers.get("x-original-client-ip", "unknown").strip()
    if client_ip == "unknown":
        client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    # 本文の整形とチェック
    clean_body = post.body.strip()

    if not clean_body or len(clean_body) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    if len(clean_body) > 200:
        raise HTTPException(status_code=400, detail="本文は200文字以下で入力してください。")

    assigned_public_id = None
    
    # 1. public_idの取得または新規割り当て
    try:
        response = supabase.table("ip_to_id") \
            .select("public_id") \
            .eq("ip_address", client_ip) \
            .limit(1) \
            .execute()
        
        ip_data = response.data
            
        if ip_data and len(ip_data) > 0:
            assigned_public_id = ip_data[0].get('public_id')
        
    except Exception as e:
        print(f"IP lookup error: {e}")
        raise HTTPException(status_code=500, detail="ID検索中にデータベースエラーが発生しました。")
    
    if not assigned_public_id:
        new_public_id = generate_public_id()
        
        try:
            supabase.table("ip_to_id").insert({
                "ip_address": client_ip, 
                "public_id": new_public_id
            }).execute()
            assigned_public_id = new_public_id
            
        except Exception as e:
            print(f"IP-ID insertion error (DB): {e}") 
            raise HTTPException(status_code=500, detail="ID割り当て中にエラーが発生しました。")
            
    # 2. BANリストチェック
    try:
        ban_check_response = supabase.table("ban_list") \
            .select("public_id") \
            .eq("public_id", assigned_public_id) \
            .limit(1) \
            .execute()

        if ban_check_response.data:
            raise HTTPException(status_code=403, detail="このIDは連投制限超過によりBANされています。")

    except HTTPException:
        raise
    except Exception as e:
        print(f"BAN check error: {e}")
        raise HTTPException(status_code=500, detail="BANチェック中にデータベースエラーが発生しました。")

    # 3. 連投制限チェック
    try:
        time_threshold = datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        time_threshold_iso = time_threshold.isoformat().replace('+00:00', 'Z')

        rate_log_response = supabase.table("post_activity_log") \
            .select("posted_at", count="exact") \
            .eq("public_id", assigned_public_id) \
            .gte("posted_at", time_threshold_iso) \
            .execute()

        post_count = rate_log_response.count if hasattr(rate_log_response, 'count') else 0
        
        # 5回以上投稿があればBAN処理へ
        if post_count >= RATE_LIMIT_COUNT:
            await ban_user(assigned_public_id)
            raise HTTPException(status_code=429, detail="連投制限（10秒間に5回以上）を超過したため、このIDはBANされました。")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Rate limit check error: {e}")
        raise HTTPException(status_code=500, detail="連投チェック中にデータベースエラーが発生しました。")
    
    # 4. 投稿とアクティビティログの記録
    current_time_utc = datetime.now(timezone.utc)
    current_time_iso = current_time_utc.isoformat().replace('+00:00', 'Z')
    
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": clean_body,
        "client_ip": client_ip,
        "created_at": current_time_iso,
    }
    
    try:
        # ユーザー投稿の挿入
        supabase.table("posts").insert(new_post).execute()
        
        # アクティビティログの挿入
        supabase.table("post_activity_log").insert({
            "public_id": assigned_public_id,
            "posted_at": current_time_iso
        }).execute()
        
        # 5. 「test」投稿への自動応答チェック (新規機能)
        if clean_body == "test":
            # ユーザー投稿よりわずかに遅延させて、タイムラインで後に表示されるようにする
            bot_response_time_utc = current_time_utc + timedelta(milliseconds=10)
            bot_response_iso = bot_response_time_utc.isoformat().replace('+00:00', 'Z')
            
            bot_post = {
                "public_id": "systems", 
                "name": "システム",
                "body": "起動確認完了",
                "client_ip": "0.0.0.0",
                "created_at": bot_response_iso,
            }
            
            try:
                # Botの応答を投稿
                supabase.table("posts").insert(bot_post).execute()
            except Exception as e:
                print(f"yuzu-bot test response post error: {e}")
                
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post insertion/log update: {e}") 
        raise HTTPException(status_code=500, detail="サーバーで投稿処理中にエラーが発生しました。")

# 投稿取得エンドポイント
@app.get("/posts")
def get_posts(ip: Optional[str] = Query(None)):
    try:
        query = supabase.table("posts") \
            .select("public_id, name, body, created_at") \
            .order("created_at", desc=True)

        if ip:
            ip_response = supabase.table("ip_to_id") \
                .select("public_id") \
                .eq("ip_address", ip) \
                .limit(1) \
                .execute()
            
            ip_data = ip_response.data
            
            if ip_data and len(ip_data) > 0:
                target_public_id = ip_data[0].get('public_id')
                query = query.eq("public_id", target_public_id)
            else:
                return {"posts": []}

        response = query.execute()

        return {"posts": response.data}
    
    except Exception as e:
        print(f"Error fetching posts: {e}")
        raise HTTPException(status_code=500, detail="投稿の取得中にエラーが発生しました。")
