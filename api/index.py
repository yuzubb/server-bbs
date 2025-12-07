import os
import random
import string
import base64
from io import BytesIO
from PIL import Image
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel, field_validator
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware

MAX_IMAGE_DIMENSION = 512
JPEG_QUALITY = 20

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

RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW_SECONDS = 10

class PostData(BaseModel):
    name: str = "匿名"
    body: str = ""
    image_base64: Optional[str] = None
    
    @field_validator('body', mode='before')
    def check_body_and_image(cls, v, values):
        image_data = values.data.get('image_base64')
        clean_body = (v or "").strip()
        
        if not clean_body and not image_data:
            raise ValueError("本文または画像データのどちらか一方は必須です。")
        
        if clean_body and len(clean_body) > 200:
            raise ValueError("本文は200文字以下で入力してください。")

        return clean_body

def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

async def ban_user(public_id: str):
    ban_reason = "連投制限（10秒間に5回以上）による自動BAN"
    
    try:
        supabase.table("ban_list").insert({
            "public_id": public_id,
            "reason": ban_reason
        }).execute()
        print(f"BAN success: public_id {public_id} banned.")
    except Exception as e:
        print(f"BAN list insertion error (could be duplicate): {e}")

    notification_post = {
        "public_id": "system",
        "name": "システム",
        "body": f"ID: {public_id} を連投制限超過のためBANしました。",
        "client_ip": "0.0.0.0",
        "created_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    
    try:
        supabase.table("posts").insert(notification_post).execute()
    except Exception as e:
        print(f"yuzu-bot notification post error: {e}")

def compress_and_re_encode_base64(data_uri: str) -> str:
    
    try:
        _, encoded_data = data_uri.split(',', 1)
    except ValueError:
        raise ValueError("無効なBase64 Data URI形式です。")

    try:
        decoded_image_data = base64.b64decode(encoded_data)
    except Exception:
        raise ValueError("Base64データのデコードに失敗しました。")

    try:
        img = Image.open(BytesIO(decoded_image_data))
    except Exception:
        raise ValueError("画像として認識できませんでした。")

    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    
    output_buffer = BytesIO()
    
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
        
    img.save(output_buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    
    compressed_encoded_data = base64.b64encode(output_buffer.getvalue()).decode('utf-8')
    
    new_data_uri = f"data:image/jpeg;base64,{compressed_encoded_data}"
    
    return new_data_uri


@app.post("/post")
async def create_post(post: PostData, request: Request):
    
    client_ip = request.headers.get("x-original-client-ip", "unknown").strip()
    if client_ip == "unknown":
        client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    clean_body = post.body.strip()

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
            raise HTTPException(status_code=500, detail="ID割り当て中にエラーが発生しました。")
            
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

    try:
        time_threshold = datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        time_threshold_iso = time_threshold.isoformat().replace('+00:00', 'Z')

        rate_log_response = supabase.table("post_activity_log") \
            .select("posted_at", count="exact") \
            .eq("public_id", assigned_public_id) \
            .gte("posted_at", time_threshold_iso) \
            .execute()

        post_count = rate_log_response.count if hasattr(rate_log_response, 'count') else 0
        
        if post_count >= RATE_LIMIT_COUNT:
            await ban_user(assigned_public_id)
            raise HTTPException(status_code=429, detail="連投制限（10秒間に5回以上）を超過したため、このIDはBANされました。")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Rate limit check error: {e}")
        raise HTTPException(status_code=500, detail="連投チェック中にデータベースエラーが発生しました。")
    
    post_body_to_save = clean_body
    is_image_post = False
    
    if post.image_base64:
        try:
            compressed_data_uri = compress_and_re_encode_base64(post.image_base64)
            post_body_to_save = compressed_data_uri
            is_image_post = True

            if len(post_body_to_save) > 500000:
                 raise HTTPException(status_code=400, detail="画像を極限まで圧縮しましたが、データがまだ大きすぎます。より小さな画像を試してください。")

        except ValueError as ve:
            raise HTTPException(status_code=400, detail=f"画像処理エラー: {ve}")
        except Exception as e:
            print(f"Image compression error: {e}")
            raise HTTPException(status_code=500, detail="画像処理中に予期せぬエラーが発生しました。")

    current_time_utc = datetime.now(timezone.utc)
    current_time_iso = current_time_utc.isoformat().replace('+00:00', 'Z')
    
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": post_body_to_save,
        "client_ip": client_ip,
        "created_at": current_time_iso,
    }
    
    try:
        supabase.table("posts").insert(new_post).execute()
        
        supabase.table("post_activity_log").insert({
            "public_id": assigned_public_id,
            "posted_at": current_time_iso
        }).execute()
        
        if clean_body == "test" and not is_image_post:
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
                supabase.table("posts").insert(bot_post).execute()
            except Exception as e:
                print(f"yuzu-bot test response post error: {e}")
                
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post insertion/log update: {e}") 
        raise HTTPException(status_code=500, detail="サーバーで投稿処理中にエラーが発生しました。")

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
