import os
import random
import string
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware 

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

# Service Role Key を使用するため、安全に管理者権限でクライアントを作成
supabase: Client = create_client(supabase_url, supabase_key)
POSTS_LIMIT = 100
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
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    assigned_public_id = None
    
    # 1. IPから既存の公開IDを検索
    try:
        ip_data, _ = supabase.table("ip_to_id") \
            .select("public_id") \
            .eq("ip_address", client_ip) \
            .single() \
            .execute()
            
        if ip_data:
            assigned_public_id = ip_data.get('public_id')
        
    except Exception:
        pass

    # 2. IDが存在しない場合、新しいIDを生成して紐づけを保存
    if not assigned_public_id:
        new_public_id = generate_public_id()
        
        try:
            supabase.table("ip_to_id").insert({
                "ip_address": client_ip, 
                "public_id": new_public_id
            }).execute()
            assigned_public_id = new_public_id
        except Exception as e:
            # 競合やその他のDBエラーの場合に備えてログを残す
            print(f"IP-ID insertion error: {e}")
            raise HTTPException(status_code=500, detail="ID割り当て中にエラーが発生しました。")
        
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip,
        "created_at": datetime.now().isoformat(),
    }
    
    # 3. 投稿を保存し、100件制限を処理
    try:
        supabase.table("posts").insert(new_post).execute()
        
        # 100件制限のチェックと削除
        count_response = supabase.table("posts").select("id", count="exact").execute()
        current_count = 0
        if len(count_response) > 1 and isinstance(count_response[1], dict):
            current_count = count_response[1].get('count', 0)
        else:
            current_count = len(count_response[0]) if count_response and count_response[0] else 0

        if current_count > POSTS_LIMIT:
            posts_to_delete_count = current_count - POSTS_LIMIT
            
            oldest_posts, _ = supabase.table("posts") \
                .select("id") \
                .order("created_at", desc=False) \
                .limit(posts_to_delete_count) \
                .execute()
            
            if oldest_posts:
                oldest_ids = [p['id'] for p in oldest_posts]
                supabase.table("posts").delete().in_("id", oldest_ids).execute()
        
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post/cull: {e}") 
        raise HTTPException(status_code=500, detail="サーバーで投稿処理中にエラーが発生しました。")


@app.get("/posts")
def get_posts():
    try:
        response = supabase.table("posts") \
            .select("public_id, name, body, created_at") \
            .order("created_at", desc=True) \
            .limit(POSTS_LIMIT) \
            .execute()

        return {"posts": response.data}
    
    except Exception as e:
        print(f"Error fetching posts: {e}")
        raise HTTPException(status_code=500, detail="投稿の取得中にエラーが発生しました。")
