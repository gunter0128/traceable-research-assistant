# 測試共用的小工具：登入、建 workspace、照前端的方式上傳、假的 OpenAI、假的 S3
# 每個測試檔都要做的準備步驟放在這裡，各檔直接 import，不用各自再寫一份
# 這裡只放一般函式（不是 fixture）：在測試裡看到呼叫，按一下就能跳到這裡看它做什麼

# 測試帳號共用的密碼
PASSWORD = "pw123456"


# 註冊 + 登入一個使用者，回傳帶著 token 的 header，直接放進 client.get(..., headers=...)
def login_headers(client, email: str) -> dict:
    client.post("/auth/register", json={"email": email, "password": PASSWORD})
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# 用這個使用者建一個 workspace，回傳它的編號
def create_workspace(client, headers: dict, name: str = "研究") -> int:
    response = client.post("/workspaces", json={"name": name}, headers=headers)
    return response.json()["id"]


# ---------- 假的 S3 ----------
# 一個 dict，key → 檔案內容
# conftest.py 的 fake_s3 每個測試開始前把它清空，再把 s3_client 的呼叫都換成讀寫這個 dict
bucket: dict[str, bytes] = {}


# 照前端的方式上傳（docs/decisions/013）：1. 跟 API 要上傳許可  2. 「直接傳到 S3」（這裡是放進假的 bucket）  3. 告訴 API 傳好了
# 回傳第 3 步的回應；第 1 步就被擋下來的話（例如不是 PDF、太大、用量到上限），回傳第 1 步的回應
def upload_pdf(client, headers, workspace_id, content, filename="doc.pdf"):
    started = client.post(
        f"/workspaces/{workspace_id}/uploads",
        json={"filename": filename, "size_bytes": len(content)},
        headers=headers,
    )
    if started.status_code != 200:
        return started
    key = started.json()["key"]
    bucket[key] = content
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        json={"key": key, "filename": filename},
        headers=headers,
    )


# ---------- 假的 OpenAI ----------
# 用 monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts) 換掉真的：不花錢、不用網路、每次結果一樣
# **kwargs：真的函式還有其他參數（例如 on_usage），假的用不到，收下來不理它


# 文件的每一段都轉成同一個 1536 維的向量
def fake_embed_texts(texts, **kwargs):
    return [[0.1] * 1536 for _ in texts]


# 問題轉成同一個 1536 維的向量
def fake_embed_query(text, **kwargs):
    return [0.1] * 1536


# 回傳一個「不管怎麼呼叫都丟出 error」的假函式，用來模擬外部服務出錯
# 例如 monkeypatch.setattr(s3_client, "download_file", always_raise(s3_client.FileMissing("NoSuchKey")))
def always_raise(error: Exception):
    def fake(*args, **kwargs):
        raise error

    return fake
