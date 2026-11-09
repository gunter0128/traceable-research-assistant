# 測試共用的小工具：登入、建 workspace、照前端的方式上傳、假的 OpenAI、假的 S3、收集 log
# 每個測試檔都要做的準備步驟放在這裡，各檔直接 import，不用各自再寫一份
# 這裡只放一般函式和類別（不是 fixture）：在測試裡看到呼叫，按一下就能跳到這裡看它做什麼

import json
import logging

import httpx2
import openai

from app.core import log, openai_client
from app.core.openai_client import EMBEDDING_MODEL, Usage

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


# 跟真的 embed_texts 一樣回傳 (向量, 用量)：每一段都轉成同一個 1536 維的向量，用量記 0 個 token
def fake_embed_texts(texts):
    return [[0.1] * 1536 for _ in texts], Usage(EMBEDDING_MODEL, 0)


# 跟真的 embed_query 一樣回傳 (向量, 用量)：問題轉成同一個 1536 維的向量，用量記 0 個 token
def fake_embed_query(text):
    return [0.1] * 1536, Usage(EMBEDDING_MODEL, 0)


# 跟真的 generate_answer 一樣回傳 (答案, 用量)：用量記 0 個 token
# 用法：monkeypatch.setattr(openai_client, "generate_answer", lambda system, user, timeout: fake_answer("答案[1]"))
def fake_answer(text):
    return text, Usage(openai_client.GENERATION_MODEL, 0, 0)


# 回傳一個「不管怎麼呼叫都丟出 error」的假函式，用來模擬外部服務出錯
# 例如 monkeypatch.setattr(s3_client, "download_file", always_raise(s3_client.FileMissing("NoSuchKey")))
def always_raise(error: Exception):
    def fake(*args, **kwargs):
        raise error

    return fake


# ---------- 假的 OpenAI 伺服器 ----------
# 用真的 OpenAI SDK，只把網路換成假的（httpx2.MockTransport）：SDK 的重試、等待上限、錯誤都是真的在跑，
# 但請求不會送到 OpenAI、不花錢。用在 test_embed_texts.py、test_qa_openai_calls.py


# 照順序回應清單裡的東西，清單用完了就一直回最後一個（例如「一直 503」）
# 清單裡放例外的話就丟出來，模擬連不上、超時
class FakeOpenAIServer:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    # MockTransport 每收到一個請求就呼叫這裡
    def handle(self, request):
        self.requests.append(request)
        result = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    # 第 i 個請求送出去的內容（JSON 轉回 dict）
    def sent(self, i=0):
        return json.loads(self.requests[i].content)


# 把 openai_client 的某個 client（"embedding_client" 或 "answer_client"）換成連到假伺服器的真 SDK client
# 網址、max_retries=0 跟正式的 client 一樣（_client_for），只有網路是假的
def use_fake_openai(monkeypatch, client_name, *responses, base_url="https://api.openai.com/v1"):
    server = FakeOpenAIServer(list(responses))
    client = openai.OpenAI(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(server.handle)),
    )
    monkeypatch.setattr(openai_client, client_name, client)
    return server


# OpenAI 錯誤的回應：狀態碼 + 錯誤代碼（例如 insufficient_quota）+ header（例如 retry-after）
def openai_error(status, code=None, headers=None):
    return httpx2.Response(status, headers=headers, json={"error": {"message": "模擬錯誤", "code": code}})


# ---------- 收集 log ----------
# 把 "app" 這個 logger 寫出的每一筆，用正式的 JSON 格式轉好、收集到一個 list 裡給測試檢查
# 測試裡用 conftest.py 的 logs fixture 拿到它（fixture 負責掛上、測試完拿下來）
class LogCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(log.JsonFormatter())
        self.entries = []

    def emit(self, record):
        self.entries.append(json.loads(self.format(record)))

    # 某個事件的所有紀錄，例如 logs.named("lease_lost")
    def named(self, event):
        return [entry for entry in self.entries if entry["event"] == event]

    # 所有 ERROR 等級的紀錄：告警看的就是這個（deploy/setup_monitoring.py 的 $.level = "ERROR"）
    def errors(self):
        return [entry for entry in self.entries if entry["level"] == "ERROR"]
