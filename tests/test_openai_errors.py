# 呼叫 OpenAI 出錯時的處理（docs/decisions/005）：錯誤分類、文件處理延後重試、問答回 503、批次送
# 真的 OpenAI 都換成假的函式，用丟出不同錯誤來模擬各種狀況

import io
import json
import time

import httpx2
import openai
import pytest

from app import worker
from app.core import openai_client, s3_client, sqs_client
from app.core.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import document_service
from tests.pdf_fixtures import valid_pdf_bytes


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts, **kwargs: [[0.1] * 1536 for _ in texts])
    monkeypatch.setattr(openai_client, "embed_query", lambda text, **kwargs: [0.1] * 1536)


# 切換成雲端模式（有 SQS），送出去的工單記在 list 裡
@pytest.fixture
def cloud(monkeypatch):
    messages = []
    monkeypatch.setattr(settings, "sqs_queue_url", "https://fake-queue")
    monkeypatch.setattr(sqs_client, "send_process_message", messages.append)
    return messages


def _register_and_login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    response = client.post("/auth/login", json={"email": email, "password": "pw123456"})
    return response.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _upload(client, email):
    token = _register_and_login(client, email)
    ws_id = client.post("/workspaces", json={"name": "研究"}, headers=_auth_headers(token)).json()["id"]
    response = client.post(
        f"/workspaces/{ws_id}/documents",
        headers=_auth_headers(token),
        files={"file": ("doc.pdf", io.BytesIO(valid_pdf_bytes()), "application/pdf")},
    )
    return token, ws_id, response.json()["id"]


def _raise(exc):
    def fake(*args, **kwargs):
        raise exc

    return fake


# 造出 OpenAI SDK 真的會丟的錯誤物件（它們需要一個假的 HTTP 回應才建得起來）
def _status_error(error_class, status, code=None, headers=None):
    request = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx2.Response(status, request=request, headers=headers)
    body = {"code": code} if code else None
    return error_class("模擬錯誤", response=response, body=body)


# ---------- 錯誤分類 ----------


@pytest.mark.parametrize(
    "exc",
    [
        openai.APIConnectionError(request=httpx2.Request("POST", "https://x")),
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.InternalServerError, 500),
    ],
)
# 測試：網路斷線、速率限制、伺服器錯誤 → 暫時性
def test_temporary_errors(exc):
    assert isinstance(openai_client._classify(exc), openai_client.OpenAITemporaryError)


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(openai.AuthenticationError, 401),
        _status_error(openai.BadRequestError, 400),
        # 陷阱：餘額不足也是 429，但等多久都不會好
        _status_error(openai.RateLimitError, 429, code="insufficient_quota"),
    ],
)
# 測試：key 無效、格式錯誤、餘額不足 → 永久性
def test_permanent_errors(exc):
    assert isinstance(openai_client._classify(exc), openai_client.OpenAIPermanentError)


# ---------- 文件處理 ----------


def _pending_document(db_session, document_id, attempts):
    document = db_session.get(Document, document_id)
    document.status = "uploaded"
    document.attempts = attempts
    db_session.commit()
    return document


# 測試：雲端、OpenAI 暫時連不上、還有剩餘次數 → 不標失敗，留在處理中並提示，交給 worker 延後
def test_temporary_error_defers_in_cloud(client, db_session, monkeypatch, cloud):
    _, _, document_id = _upload(client, "alice@example.com")
    document = _pending_document(db_session, document_id, attempts=0)
    monkeypatch.setattr(openai_client, "embed_texts", _raise(openai_client.OpenAITemporaryError()))

    with pytest.raises(document_service.RetryLater):
        document_service.process_document(db_session, document)

    db_session.refresh(document)
    assert document.status == "uploaded"
    assert document.error_message == "AI 服務暫時無法使用，稍後會自動重試"
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0


# 測試：第 3 回合還是暫時連不上 → 標失敗，請使用者稍後重試
def test_temporary_error_fails_on_last_attempt(client, db_session, monkeypatch, cloud):
    _, _, document_id = _upload(client, "bob@example.com")
    document = _pending_document(db_session, document_id, attempts=document_service.MAX_ATTEMPTS - 1)
    monkeypatch.setattr(openai_client, "embed_texts", _raise(openai_client.OpenAITemporaryError()))

    document_service.process_document(db_session, document)

    assert document.status == "failed"
    assert document.error_message == "AI 服務暫時無法使用，已自動重試 3 次，請稍後按重試"


# 測試：本機沒有 SQS 沒辦法延後 → 直接標失敗
def test_temporary_error_fails_locally(client, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", _raise(openai_client.OpenAITemporaryError()))

    token, _, document_id = _upload(client, "carol@example.com")

    body = client.get(f"/documents/{document_id}", headers=_auth_headers(token)).json()
    assert body["status"] == "failed"
    assert body["error_message"] == "AI 服務暫時無法使用，請稍後按重試"


# 測試：永久性錯誤（例如 key 無效）→ 不重試，直接標失敗，請人處理
def test_permanent_error_fails_immediately(client, db_session, monkeypatch, cloud):
    _, _, document_id = _upload(client, "dave@example.com")
    document = _pending_document(db_session, document_id, attempts=0)
    monkeypatch.setattr(openai_client, "embed_texts", _raise(openai_client.OpenAIPermanentError()))

    document_service.process_document(db_session, document)

    assert document.status == "failed"
    assert document.error_message == "系統設定有誤，請聯絡管理員"


# 測試：worker 收到 RetryLater → 叫 SQS 2 分鐘後再送這張工單，並讓這次執行失敗（工單才不會被刪）
def test_worker_delays_message_on_retry_later(client, db_session, monkeypatch, cloud):
    _, _, document_id = _upload(client, "erin@example.com")
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(openai_client, "embed_texts", _raise(openai_client.OpenAITemporaryError()))
    delayed = []
    monkeypatch.setattr(sqs_client, "delay_message", lambda receipt, seconds: delayed.append((receipt, seconds)))
    event = {"Records": [{"body": json.dumps({"document_id": document_id}), "receiptHandle": "receipt-1"}]}

    with pytest.raises(document_service.RetryLater):
        worker.handle_process_message(event, None)

    assert delayed == [("receipt-1", 120)]


# 測試：批次送 → 250 段、每批 100 段，只呼叫 3 次，chunk 的順序編號照原本的順序 0~249
def test_embeddings_sent_in_batches(client, db_session, monkeypatch):
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append(len(texts))
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(openai_client, "embed_texts", fake_embed)
    monkeypatch.setattr(document_service.chunker, "chunk_text", lambda text: [f"段落 {i}" for i in range(250)])

    _, _, document_id = _upload(client, "frank@example.com")

    assert calls == [100, 100, 50]
    chunks = db_session.query(Chunk).filter(Chunk.document_id == document_id).order_by(Chunk.chunk_index).all()
    assert [c.chunk_index for c in chunks] == list(range(250))
    assert chunks[123].content == "段落 123"


# ---------- 問答 ----------


# 測試：問答時 OpenAI 暫時連不上 → 503，請稍後再試
def test_ask_returns_503_when_ai_unavailable(client, monkeypatch):
    token, ws_id, _ = _upload(client, "grace@example.com")
    monkeypatch.setattr(openai_client, "generate_answer", _raise(openai_client.OpenAITemporaryError()))

    response = client.post(f"/workspaces/{ws_id}/ask", json={"question": "?"}, headers=_auth_headers(token))

    assert response.status_code == 503
    assert response.json()["detail"] == "AI 服務暫時無法使用，請稍後再試"


# 測試：問答時遇到永久性錯誤 → 503，訊息不同
def test_ask_returns_503_when_misconfigured(client, monkeypatch):
    token, ws_id, _ = _upload(client, "henry@example.com")
    monkeypatch.setattr(openai_client, "embed_query", _raise(openai_client.OpenAIPermanentError()))

    response = client.post(f"/workspaces/{ws_id}/ask", json={"question": "?"}, headers=_auth_headers(token))

    assert response.status_code == 503
    assert response.json()["detail"] == "系統設定有誤，請聯絡管理員"


# ---------- 自己寫的重試迴圈（docs/decisions/008）----------
# 真的等待（time.sleep）換成只記錄「要等幾秒」，測試才不用真的等


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(openai_client.time, "sleep", waited.append)
    # 隨機的 jitter 固定成 0，間隔就會剛好是 0.5、1、2……方便檢查
    monkeypatch.setattr(openai_client.random, "random", lambda: 0)
    return waited


# 依序丟出（或回傳）清單裡的東西，模擬「前幾次失敗、後來成功」
def _calls(*results):
    record = []

    def call(timeout):
        record.append(timeout)
        result = results[len(record) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    return call, record


def _connection_error():
    return openai.APIConnectionError(request=httpx2.Request("POST", "https://x"))


def _retry(call, **overrides):
    options = {
        "attempts": 5,
        "timeout": 30,
        "max_wait": 60,
        "give_up_on_long_wait": False,
    }
    options.update(overrides)
    return openai_client._with_retries(call, **options)


# 測試：暫時性錯誤 → 重試，間隔 0.5、1 秒越來越長，第 3 次成功
def test_retries_with_growing_backoff(sleeps):
    call, record = _calls(_connection_error(), _connection_error(), "ok")

    assert _retry(call) == "ok"
    assert len(record) == 3
    assert sleeps == [0.5, 1.0]


# 測試：永久性錯誤 → 不重試，馬上放棄
def test_permanent_error_is_not_retried(sleeps):
    call, record = _calls(_status_error(openai.AuthenticationError, 401))

    with pytest.raises(openai_client.OpenAIPermanentError):
        _retry(call)
    assert len(record) == 1
    assert sleeps == []


# 測試：試滿次數都失敗 → 放棄，丟暫時性錯誤
def test_gives_up_after_all_attempts(sleeps):
    call, record = _calls(*[_connection_error() for _ in range(5)])

    with pytest.raises(openai_client.OpenAITemporaryError):
        _retry(call)
    assert len(record) == 5


# 測試：OpenAI 說「請等 20 秒」→ 照它說的等
def test_follows_retry_after(sleeps):
    call, _ = _calls(_status_error(openai.RateLimitError, 429, headers={"retry-after": "20"}), "ok")

    _retry(call)

    assert sleeps == [20.0]


# 測試：文件處理，OpenAI 說「請等 100 秒」→ 超過上限，只等 60 秒就重試
def test_processing_caps_long_retry_after(sleeps):
    call, _ = _calls(_status_error(openai.RateLimitError, 429, headers={"retry-after": "100"}), "ok")

    _retry(call, max_wait=60, give_up_on_long_wait=False)

    assert sleeps == [60]


# 測試：問答，OpenAI 說「請等 30 秒」→ 超過 5 秒，不等了，直接放棄讓使用者知道
def test_qa_gives_up_on_long_retry_after(sleeps):
    call, record = _calls(_status_error(openai.RateLimitError, 429, headers={"retry-after": "30"}), "ok")

    with pytest.raises(openai_client.OpenAITemporaryError):
        _retry(call, attempts=2, max_wait=5, give_up_on_long_wait=True)
    assert len(record) == 1
    assert sleeps == []


# 測試：時間預算快用完 → 連試都不試，直接放棄
def test_stops_when_deadline_passed(sleeps):
    call, record = _calls("ok")

    with pytest.raises(openai_client.OpenAITemporaryError):
        _retry(call, deadline=time.monotonic() + 0.5)
    assert record == []


# 測試：每次嘗試最多等的秒數，不會超過剩下的時間預算
def test_attempt_timeout_shrinks_to_fit_deadline(sleeps):
    call, record = _calls("ok")

    _retry(call, timeout=15, deadline=time.monotonic() + 8)

    assert record[0] <= 8


# 測試：等完之後會超過時間預算 → 不等了，直接放棄
def test_does_not_wait_past_deadline(sleeps):
    call, record = _calls(_status_error(openai.RateLimitError, 429, headers={"retry-after": "4"}), "ok")

    with pytest.raises(openai_client.OpenAITemporaryError):
        _retry(call, max_wait=5, give_up_on_long_wait=True, deadline=time.monotonic() + 3)
    assert len(record) == 1
    assert sleeps == []


# 測試：每次嘗試之前都會呼叫 on_attempt（文件處理用它報平安）
def test_calls_on_attempt_before_each_try(sleeps):
    call, _ = _calls(_connection_error(), _connection_error(), "ok")
    heartbeats = []

    _retry(call, on_attempt=lambda: heartbeats.append(1))

    assert len(heartbeats) == 3
