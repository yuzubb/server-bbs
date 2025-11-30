# api/index.py
import os
import random
import string
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware # ★ CORSのために追加 ★

# --- Supabaseクライアントの初期化 ---
# 環境変数から接続情報を取得
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    # Vercel環境変数設定が必須
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

# Supabaseクライアントを作成
supabase: Client = create_client(supabase_url, supabase_key)
POSTS_LIMIT = 100
app = FastAPI()

# --- CORS設定 ---
# フロントエンドからのアクセスを許可するための設定
app.add_middleware(
    CORSMiddleware,
    # 開発中は "*" で全て許可、本番ではフロントエンドのURLに限定するのが安全です。
    # VercelのURLとローカル環境を許可
    allow_origins=["https://server-bbs.vercel.app", "http://localhost:3000", "http://127.0.0.1:8000", "*"], 
    allow_credentials=True,
    allow_methods=["*"], # 全てのメソッド (GET, POSTなど) を許可
    allow_headers=["*"], # 全てのヘッダーを許可
)

# --- スキーマ定義 ---
class PostData(BaseModel):
    name: str = "匿名"  # 名前は任意
    body: str         # 本文は必須

# 7文字の公開IDを生成する関数
def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# --- 投稿作成エンドポイント (POST /post) ---
@app.post("/post")
async def create_post(post: PostData, request: Request):
    # 1. IPアドレスの取得 (Vercel/CDN環境の標準ヘッダー)
    # 複数IPが含まれる可能性があるため、最初のエントリを使用
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    new_post = {
        "public_id": generate_public_id(),
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip, # ★ 非公開でデータベースに記録 ★
        "created_at": datetime.now().isoformat(),
    }
    
    try:
        # 2. 投稿をデータベースに保存
        supabase.table("posts").insert(new_post).execute()
        
        # 3. 【100件制限ロジック】古い投稿の自動削除
        count_data, _ = supabase.table("posts").select("id", count="exact").execute()
        current_count = count_data[1].get('count', 0)

        if current_count > POSTS_LIMIT:
            posts_to_delete_count = current_count - POSTS_LIMIT
            
            # 最も古い投稿のIDを posts_to_delete_count 件取得
            oldest_posts, _ = supabase.table("posts") \
                .select("id") \
                .order("created_at", desc=False) \
                .limit(posts_to_delete_count) \
                .execute()
            
            # 取得したIDを基に削除
            if oldest_posts:
                oldest_ids = [p['id'] for p in oldest_posts]
                supabase.table("posts").delete().in_("id", oldest_ids).execute()
                # print(f"Deleted {len(oldest_ids)} oldest posts.") # Vercelログに出力される
        
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        # データベースエラーを詳細に出力 (デバッグ用)
        print(f"Database error: {e}") 
        # ユーザーには一般的なエラーメッセージを返す
        raise HTTPException(status_code=500, detail="サーバーでエラーが発生しました。Supabaseの設定を確認してください。")


# --- 投稿一覧取得エンドポイント (GET /posts) ---
@app.get("/posts")
def get_posts():
    try:
        # 最新の100件を投稿時間順に取得 (created_at の降順)
        # RLSとSELECT句により、client_ip は含まれないことが保証されます
        response = supabase.table("posts") \
            .select("public_id, name, body, created_at") \
            .order("created_at", desc=True) \
            .limit(POSTS_LIMIT) \
            .execute()

        # Supabaseの結果はタプルで返されるため、data部分のみを抽出
        return {"posts": response.data}
    
    except Exception as e:
        print(f"Error fetching posts: {e}")
        raise HTTPException(status_code=500, detail="投稿の取得中にエラーが発生しました。")
