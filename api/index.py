import os
import random
import string
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(supabase_url, supabase_key)
POSTS_LIMIT = 100
app = FastAPI()

class PostData(BaseModel):
    name: str = "匿名"
    body: str

def generate_public_id(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

@app.post("/post")
async def create_post(post: PostData, request: Request):
    client_ip = request.headers.get("x-forwarded-for", "unknown")

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
        supabase.table("posts").insert(new_post).execute()

        count_data, count_error = supabase.table("posts").select("id", count="exact").execute()
        current_count = count_data[1].get('count', 0)

        if current_count > POSTS_LIMIT:
            posts_to_delete_count = current_count - POSTS_LIMIT

            oldest_posts = supabase.table("posts") \
                .select("id") \
                .order("created_at", desc=False) \
                .limit(posts_to_delete_count) \
                .execute()

            if oldest_posts.data:
                oldest_ids = [p['id'] for p in oldest_posts.data]
                supabase.table("posts").delete().in_("id", oldest_ids).execute()
                print(f"Deleted {len(oldest_ids)} oldest posts.")

        return {"message": "投稿が完了しました", "public_id": new_post["public_id"]}

    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="サーバーでエラーが発生しました。")


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
