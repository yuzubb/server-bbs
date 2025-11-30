import os
import random
import string
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware 

# 環境変数の取得
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

# Supabaseクライアントの初期化
supabase: Client = create_client(supabase_url, supabase_key)

# 投稿数制限を解除するため、POSTS_LIMITの定義を削除し、関連ロジックを修正します。

app = FastAPI()

# CORSミドルウェアの設定
app.add_middleware(
    CORSMiddleware,
    # 許可するオリジンリスト
    allow_origins=["https://server-bbs.vercel.app", "http://localhost:3000", "http://127.0.0.1:8000", "*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 投稿データのスキーマ定義
class PostData(BaseModel):
    name: str = "匿名"
    body: str

# 公開ID（識別子）を生成する関数
def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# 投稿作成エンドポイント
@app.post("/post")
async def create_post(post: PostData, request: Request):
    # クライアントIPアドレスの取得
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(',')[0].strip()
    
    # 本文のバリデーション
    if not post.body or len(post.body.strip()) == 0:
        raise HTTPException(status_code=400, detail="本文は必須です。")
        
    assigned_public_id = None
    
    # 既存のpublic_idをIPアドレスから検索
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
    
    # public_idが未割り当ての場合、新しく生成して保存
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
            
    # 新しい投稿データを作成
    new_post = {
        "public_id": assigned_public_id,
        "name": post.name.strip() or "匿名",
        "body": post.body.strip(),
        "client_ip": client_ip,
        "created_at": datetime.now().isoformat(),
    }
    
    try:
        # 投稿をデータベースに挿入
        supabase.table("posts").insert(new_post).execute()
        
        # 投稿数を無制限にするため、古い投稿を削除するロジック（culling logic）は削除します。
        
        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error during post insertion: {e}") 
        raise HTTPException(status_code=500, detail="サーバーで投稿処理中にエラーが発生しました。")

# 投稿取得エンドポイント
@app.get("/posts")
def get_posts():
    try:
        # 投稿数制限を解除するため、.limit(POSTS_LIMIT)を削除しました。
        # これにより、すべての投稿が取得されます。
        response = supabase.table("posts") \
            .select("public_id, name, body, created_at") \
            .order("created_at", desc=True) \
            .execute()

        return {"posts": response.data}
    
    except Exception as e:
        print(f"Error fetching posts: {e}")
        raise HTTPException(status_code=500, detail="投稿の取得中にエラーが発生しました。")
