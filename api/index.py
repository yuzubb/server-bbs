# api/index.py
import os
import random
import string
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware 

# --- Supabaseクライアントの初期化 ---
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(supabase_url, supabase_key)
POSTS_LIMIT = 100
app = FastAPI()

# --- CORS設定 ---
app.add_middleware(
    CORSMiddleware,
    # フロントエンドのURLとローカル環境を許可。最終的には厳密なURLに限定推奨。
    allow_origins=["https://server-bbs.vercel.app", "http://localhost:3000", "http://127.0.0.1:8000", "*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- スキーマ定義 ---
class PostData(BaseModel):
    name: str = "匿名"
    body: str

# 7文字の公開IDを生成する関数
def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# --- 投稿作成エンドポイント (POST /post) ---
@app.post("/post")
async def create_post(post: PostData, request: Request):
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    new_post = {
        "public_id": generate_public_id(),
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip,
        "created_at": datetime.now().isoformat(),
    }
    
    try:
        # 1. 投稿をデータベースに保存
        supabase.table("posts").insert(new_post).execute()
        
        # 2. 【100件制限ロジック】古い投稿の自動削除
        count_response = supabase.table("posts").select("id", count="exact").execute()
        
        # ★★★ 修正箇所: countメタデータを安全に抽出 ★★★
        current_count = 0
        if len(count_response) > 1 and isinstance(count_response[1], dict):
            # 応答タプルの2番目の要素がメタデータ（辞書）である場合
            current_count = count_response[1].get('count', 0)
        else:
            # カウント情報が抽出できなかった場合、データ部分の長さを使用 (フォールバック)
            current_count = len(count_response[0]) if count_response and count_response[0] else 0
        # ★★★ 修正箇所ここまで ★★★

        if current_count > POSTS_LIMIT:
            posts_to_delete_count = current_count - POSTS_LIMIT
            
            # 最も古い投稿のIDを取得し、削除
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
        print(f"Database error: {e}") 
        raise HTTPException(status_code=500, detail="サーバーでエラーが発生しました。Supabaseの設定、特にRLSを確認してください。")


# --- 投稿一覧取得エンドポイント (GET /posts) ---
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
