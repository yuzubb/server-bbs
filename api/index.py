import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(supabase_url, supabase_key)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://server-bbs.vercel.app", "http://localhost:3000", "http://127.0.0.1:8000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PostData(BaseModel):
    name: str = "匿名"
    body: str

def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

COOLDOWN_SECONDS = 3
BAN_THRESHOLD = 5
BAN_WINDOW_SECONDS = 10 # ゆずbot: 10秒間に

@app.post("/post")
async def create_post(post: PostData, request: Request):
    
    # --- 1. 堅牢なクライアントIPアドレスの特定 (修正) ---
    x_forwarded_for = request.headers.get("x-forwarded-for")
    
    if x_forwarded_for:
        # X-Forwarded-Forの最初のIP（クライアントIP）を使用
        client_ip = x_forwarded_for.split(',')[0].strip()
    elif request.client:
        # フォールバックとして直接の接続元IPを使用
        client_ip = request.client.host
    else:
        client_ip = "unknown"

    if client_ip == "unknown" or not client_ip:
        raise HTTPException(status_code=400, detail="クライアントIPアドレスを特定できませんでした。")
        
    # --- 2. BANリストチェック (新規) ---
    try:
        ban_response = supabase.table("banned_ips") \
            .select("public_id") \
            .eq("ip_address", client_ip) \
            .limit(1) \
            .execute()

        if ban_response.data and len(ban_response.data) > 0:
            banned_public_id = ban_response.data[0].get('public_id')
            raise HTTPException(
                status_code=403, 
                detail=f"IPアドレス {client_ip} はアクセス禁止されています。ID {banned_public_id}。"
            )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Ban check error: {e}")
        raise HTTPException(status_code=500, detail="BANチェック中にデータベースエラーが発生しました。")


    # --- 3. 標準の連投クールダウンチェック (3秒) (既存) ---
    try:
        cooldown_response = supabase.table("post_cooldown") \
            .select("last_post_at") \
            .eq("ip_address", client_ip) \
            .limit(1) \
            .execute()
            
        cooldown_data = cooldown_response.data
        
        if cooldown_data and len(cooldown_data) > 0:
            last_post_at_str = cooldown_data[0].get('last_post_at')
            last_post_at = datetime.fromisoformat(last_post_at_str.replace('Z', '+00:00')) 
            
            now_utc = datetime.now(timezone.utc)
            
            # タイムゾーン情報を付加
            time_since_last_post = now_utc - last_post_at.replace(tzinfo=timezone.utc)
            
            if time_since_last_post.total_seconds() < COOLDOWN_SECONDS:
                wait_time = round(COOLDOWN_SECONDS - time_since_last_post.total_seconds(), 2)
                raise HTTPException(
                    status_code=429, 
                    detail=f"連投は禁止されています。あと {wait_time} 秒待ってください。"
                )
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Cooldown check error: {e}")
        raise HTTPException(status_code=500, detail="連投チェック中にデータベースエラーが発生しました。")

    # --- 4. 入力値の検証 (既存) ---
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    if len(post.body.strip()) > 200:
        raise HTTPException(status_code=400, detail="本文は200文字以下で入力してください。")

    # --- 5. IDの取得または割り当て (既存ロジックを流用) ---
    assigned_public_id = None
    
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
            raise HTTPException(status_code=500, detail="ID割り当て中にエラーが発生しました。データベースの制約違反を確認してください。")

    # --- 6. ゆずbot: 高頻度投稿チェック (5回/10秒) とBAN処理 (新規) ---
    
    now_utc = datetime.now(timezone.utc)
    ban_window_start = now_utc - timedelta(seconds=BAN_WINDOW_SECONDS)
    ban_window_start_iso = ban_window_start.isoformat().replace('+00:00', 'Z')
    
    try:
        # 過去10秒間の投稿数をカウント
        # Note: 'posts'テーブルの 'client_ip' と 'created_at' にインデックスがあることを推奨
        post_count_response = supabase.table("posts") \
            .select("id", count="exact") \
            .eq("client_ip", client_ip) \
            .gte("created_at", ban_window_start_iso) \
            .execute()
        
        current_post_count = post_count_response.count if post_count_response.count is not None else 0
        
        # 今回の投稿を含めたカウント
        total_post_count = current_post_count + 1 
        
        if total_post_count >= BAN_THRESHOLD:
            # BAN処理
            ban_record = {
                "ip_address": client_ip,
                "public_id": assigned_public_id,
                "banned_at": now_utc.isoformat().replace('+00:00', 'Z')
            }
            
            # BANテーブルに挿入 (既にBANリストにあるIPの重複挿入を回避)
            supabase.table("banned_ips").upsert(
                ban_record, 
                on_conflict="ip_address"
            ).execute()
            
            # BANが成立したので投稿は行わずにメッセージを返して終了
            raise HTTPException(
                status_code=403, 
                detail=f"ID {assigned_public_id} をBANしました"
            )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Yuzu-bot BAN check/insertion error: {e}")
        raise HTTPException(status_code=500, detail="ゆずbotによるBAN処理中にデータベースエラーが発生しました。")


    # --- 7. 投稿処理とクールダウン更新 (既存ロジックを流用) ---
    
    current_time_iso = now_utc.isoformat().replace('+00:00', 'Z')
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip,
        "created_at": current_time_iso,
    }
    
    try:
        supabase.table("posts").insert(new_post).execute()
        
        cooldown_update_data = {
            "ip_address": client_ip,
            "last_post_at": current_time_iso
        }
        
        supabase.table("post_cooldown").upsert(
            cooldown_update_data, 
            on_conflict="ip_address"
        ).execute()
        
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post insertion/cooldown update: {e}") 
        raise HTTPException(status_code=500, detail="サーバーで投稿処理中にエラーが発生しました。")

# get_posts関数は変更なし
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
