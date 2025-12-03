import os
import random
import string
from datetime import datetime
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

@app.post("/post")
async def create_post(post: PostData, request: Request):
    
    client_ip = request.headers.get("x-original-client-ip", "unknown").strip()
    
    if client_ip == "unknown":
        
        client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
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
            
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip,
        "created_at": datetime.now().isoformat(),
    }
    
    try:
        supabase.table("posts").insert(new_post).execute()
        
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post insertion: {e}") 
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
