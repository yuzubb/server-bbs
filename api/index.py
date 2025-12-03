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
    public_id: Optional[str] = None
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
        
    final_public_id = None
    
    if post.public_id and len(post.public_id.strip()) > 0:
        final_public_id = post.public_id.strip()
    else:
        final_public_id = generate_public_id()

    new_post = {
        "public_id": final_public_id,
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
def get_posts(public_id: Optional[str] = Query(None)):
    try:
        query = supabase.table("posts") \
            .select("public_id, name, body, created_at") \
            .order("created_at", desc=True)

        if public_id:
            query = query.eq("public_id", public_id)

        response = query.execute()

        return {"posts": response.data}
    
    except Exception as e:
        print(f"Error fetching posts: {e}")
        raise HTTPException(status_code=500, detail="投稿の取得中にエラーが発生しました。")
