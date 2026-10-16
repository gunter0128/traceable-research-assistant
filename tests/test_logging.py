# 結構化 log（docs/decisions/010）
# 測的是：每一筆都是 JSON、自動帶上請求編號和文件編號、上傳和背景處理串得起來、不記使用者的內容

import io
import json
import logging

import pytest

from app.core import log, openai_client, s3_client, sqs_client
from app.core.config import settings
from tests.pdf_fixtures import valid_pdf_bytes


# 把 "app" 這個 logger 寫出的每一筆，用正式的 JSON 格式轉好、收集到一個 list 裡給測試檢查
class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(log.JsonFormatter())
        self.entries = []

    def emit(self, record):
        self.entries.append(json.loads(self.format(record)))

    def named(self, event):
        return [entry for entry in self.entries if entry["event"] == event]


@pytest.fixture
def logs():
    collector = _Collector()
    logger = logging.getLogger("app")
    logger.addHandler(collector)
    yield collector
    logger.removeHandler(collector)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))

    # 跟真的 embed_texts 一樣，呼叫 OpenAI 之前先呼叫 on_attempt（試一次就成功）
    def fake_embed(texts, on_attempt=None, **kwargs):
        if on_attempt is not None:
            on_attempt()
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(openai_client, "embed_texts", fake_embed)


def _login(client, email="alice@example.com"):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    token = client.post("/auth/login", json={"email": email, "password": "pw123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _upload(client, headers, filename="secret-findings.pdf"):
    ws_id = client.post("/workspaces", json={"name": "研究"}, headers=headers).json()["id"]
    return client.post(
        f"/workspaces/{ws_id}/documents",
        headers=headers,
        files={"file": (filename, io.BytesIO(valid_pdf_bytes("retrieval " * 50)), "application/pdf")},
    )


# 測試：一筆 log 轉出來是 JSON，有固定欄位、目前的編號、和這筆自己的欄位
def test_format_is_json_with_context(logs):
    log.bind(request_id="abc123", document_id=42)

    log.event("embed_batch", batch=1, duration_ms=812)

    entry = logs.entries[-1]
    assert entry["event"] == "embed_batch"
    assert entry["level"] == "INFO"
    assert entry["request_id"] == "abc123"
    assert entry["document_id"] == 42
    assert entry["batch"] == 1
    assert "time" in entry
    log.bind()


# 測試：換下一張工單時，上一張的編號不會留著（Lambda 會重複使用同一個程式）
def test_bind_clears_previous_ids(logs):
    log.bind(request_id="first", document_id=1)
    log.bind(request_id="second")

    log.event("something")

    assert logs.entries[-1]["request_id"] == "second"
    assert "document_id" not in logs.entries[-1]


# 測試：每個請求有一筆 request log，帶狀態碼、花多久、是誰；回應的 header 有同一個請求編號
def test_request_is_logged_with_id(client, logs):
    headers = _login(client)

    response = client.get("/workspaces", headers=headers)

    entry = logs.named("request")[-1]
    assert entry["path"] == "/workspaces"
    assert entry["status"] == 200
    assert entry["user_id"] is not None
    assert isinstance(entry["duration_ms"], int)
    assert response.headers["X-Request-ID"] == entry["request_id"]


# 測試：上傳 → 處理的每一筆 log 都帶著同一個請求編號和文件編號，串得起來
def test_upload_and_processing_share_ids(client, logs):
    headers = _login(client)

    response = _upload(client, headers)  # 本機模式：上傳時直接處理完

    request_id = response.headers["X-Request-ID"]
    document_id = response.json()["id"]
    for event in ("uploaded", "claimed", "downloaded", "parsed", "chunked", "embed_batch", "processed"):
        entry = logs.named(event)[-1]
        assert entry["request_id"] == request_id, event
        assert entry["document_id"] == document_id, event
    assert logs.named("embed_batch")[-1]["openai_attempts"] == 1
    assert "total_ms" in logs.named("processed")[-1]


# 測試：雲端模式送進 SQS 的工單帶著請求編號，Lambda 才能沿用
def test_queue_message_carries_request_id(client, logs, monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "sqs_queue_url", "https://example.com/queue")
    monkeypatch.setattr(sqs_client.client, "send_message", lambda **kwargs: sent.append(kwargs))
    headers = _login(client)

    response = _upload(client, headers)

    body = json.loads(sent[0]["MessageBody"])
    assert body["document_id"] == response.json()["id"]
    assert body["request_id"] == response.headers["X-Request-ID"]


# 測試：log 裡沒有使用者的檔名和問題內容
def test_no_user_content_in_logs(client, logs, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_query", lambda text, **kwargs: [0.1] * 1536)
    monkeypatch.setattr(openai_client, "generate_answer", lambda question, context, **kwargs: "答案")
    headers = _login(client)
    ws_id = _upload(client, headers).json()["workspace_id"]

    client.post(f"/workspaces/{ws_id}/ask", json={"question": "未發表的機密問題"}, headers=headers)

    everything = json.dumps(logs.entries, ensure_ascii=False)
    assert "secret-findings" not in everything
    assert "未發表的機密問題" not in everything
    assert logs.named("qa_embed")[-1]["question_chars"] == len("未發表的機密問題")


# 測試：預料之外的錯誤會留下錯誤堆疊（給使用者的訊息很籠統，log 是唯一的線索）
def test_unexpected_error_logs_traceback(client, logs, monkeypatch):
    def broken(texts, **kwargs):
        raise RuntimeError("something odd")

    monkeypatch.setattr(openai_client, "embed_texts", broken)
    headers = _login(client)

    _upload(client, headers)

    entry = logs.named("unexpected_error")[-1]
    assert entry["level"] == "ERROR"
    assert "RuntimeError: something odd" in entry["traceback"]
    assert logs.named("failed")[-1]["error_type"] == "RuntimeError"
