# 背景處理（雲端模式）的測試：送工單、Lambda 收工單、手動重試、定期掃描
# 真的 SQS 換成一個記錄「送了哪些工單」的 list，跟 _fake_s3 用 dict 假裝 S3 是同一個道理

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import EndpointConnectionError

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
    # sweeper 現在也會對帳、列出 S3 檔案；這裡回空的，不碰真的 S3（對帳另外在 test_storage_consistency.py 測）
    monkeypatch.setattr(s3_client, "list_files", lambda prefix: [])


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts: [[0.1] * 1536 for _ in texts])


# 切換成雲端模式：設定一個假的 SQS 網址，送出去的工單全部記在 sent 這個 list 裡
# 測試結束後 monkeypatch 會自動把 settings 改回來，不影響其他測試檔（它們還是本機模式）
@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(settings, "sqs_queue_url", "https://fake-queue")
    monkeypatch.setattr(sqs_client, "send_process_message", messages.append)
    return messages


# Lambda 進入點自己會開 session，這裡換成測試用的 session，才會連到測試資料庫的同一批資料
@pytest.fixture
def worker_db(monkeypatch, db_session):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)


def _register_and_login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    response = client.post(
        "/auth/login", json={"email": email, "password": "pw123456"}
    )
    return response.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _create_workspace(client, token, name="研究"):
    response = client.post(
        "/workspaces", json={"name": name}, headers=_auth_headers(token)
    )
    return response.json()["id"]


def _upload_pdf(client, token, workspace_id):
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        headers=_auth_headers(token),
        files={"file": ("doc.pdf", io.BytesIO(valid_pdf_bytes()), "application/pdf")},
    )


# 假的送工單函式：一呼叫就丟例外，模擬 SQS 連不上
def _broken_send(document_id):
    raise EndpointConnectionError(endpoint_url="https://fake-queue")


# 組出跟真的 SQS 觸發 Lambda 時一樣格式的 event
def _sqs_event(*document_ids):
    records = []
    for document_id in document_ids:
        records.append({"body": json.dumps({"document_id": document_id})})
    return {"Records": records}


def _chunk_count(db_session, document_id):
    return db_session.query(Chunk).filter(Chunk.document_id == document_id).count()


# 測試：雲端模式上傳 → 立刻回應、還沒處理（沒有 chunk），而且送出了一張工單
def test_upload_sends_message_instead_of_processing(client, db_session, sent):
    token = _register_and_login(client, "alice@example.com")
    ws_id = _create_workspace(client, token)

    response = _upload_pdf(client, token, ws_id)

    body = response.json()
    assert response.status_code == 201
    assert body["status"] == "uploaded"
    assert sent == [body["id"]]
    assert _chunk_count(db_session, body["id"]) == 0


# 測試：工單送不出去 → 文件直接標失敗，不會永遠卡在處理中
def test_upload_marks_failed_when_send_fails(client, monkeypatch, sent):
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)

    response = _upload_pdf(client, token, ws_id)

    assert response.json()["status"] == "failed"
    assert response.json()["error_message"] == "排入處理佇列失敗，可以重試"


# 測試：Lambda 收到工單 → 文件被處理完成，chunk 存進去
def test_worker_processes_document(client, db_session, sent, worker_db):
    token = _register_and_login(client, "carol@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]

    worker.handle_process_message(_sqs_event(document_id), None)

    document = db_session.get(Document, document_id)
    db_session.refresh(document)
    assert document.status == "processed"
    assert _chunk_count(db_session, document_id) > 0


# 測試：工單送出後文件被刪掉了 → Lambda 不會出錯，直接跳過
def test_worker_skips_deleted_document(client, sent, worker_db):
    token = _register_and_login(client, "dave@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]
    client.delete(f"/documents/{document_id}", headers=_auth_headers(token))

    worker.handle_process_message(_sqs_event(document_id), None)  # 不丟例外就算通過


# 測試：次數用完的文件，就算又收到工單，也不會再處理，直接標失敗
def test_process_stops_after_max_attempts(client, db_session, monkeypatch):
    token = _register_and_login(client, "erin@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.status = "uploaded"
    document.attempts = document_service.MAX_ATTEMPTS
    db_session.commit()

    calls = []
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts: calls.append(texts))
    document_service.process_document(db_session, document)

    assert document.status == "failed"
    assert calls == []  # 完全沒有呼叫 OpenAI


# 測試：失敗的文件可以手動重試，次數歸零並重新送工單
def test_retry_failed_document(client, db_session, monkeypatch, sent):
    token = _register_and_login(client, "frank@example.com")
    ws_id = _create_workspace(client, token)
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]  # 送不出去 → failed
    monkeypatch.setattr(sqs_client, "send_process_message", sent.append)

    response = client.post(f"/documents/{document_id}/retry", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    assert sent == [document_id]
    assert db_session.get(Document, document_id).attempts == 0
    assert response.json()["error_message"] is None  # 舊的失敗原因被清掉了


# 測試：不是失敗狀態的文件不能重試（409）
def test_cannot_retry_non_failed_document(client, sent):
    token = _register_and_login(client, "grace@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]  # 處理中

    response = client.post(f"/documents/{document_id}/retry", headers=_auth_headers(token))

    assert response.status_code == 409


# 測試：不能重試別人的文件（403）
def test_cannot_retry_others_document(client, sent, monkeypatch):
    alice = _register_and_login(client, "henry@example.com")
    ws_id = _create_workspace(client, alice)
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    document_id = _upload_pdf(client, alice, ws_id).json()["id"]
    bob = _register_and_login(client, "ivy@example.com")

    response = client.post(f"/documents/{document_id}/retry", headers=_auth_headers(bob))

    assert response.status_code == 403


# 測試：定期掃描 → 卡住的（工單送出超過 30 分鐘）重送；剛送出的不動；次數用完的標失敗
def test_sweep_requeues_only_stuck_documents(client, db_session, sent, worker_db):
    token = _register_and_login(client, "jack@example.com")
    ws_id = _create_workspace(client, token)
    stuck_id = _upload_pdf(client, token, ws_id).json()["id"]
    fresh_id = _upload_pdf(client, token, ws_id).json()["id"]
    exhausted_id = _upload_pdf(client, token, ws_id).json()["id"]

    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.get(Document, stuck_id).queued_at = long_ago
    exhausted = db_session.get(Document, exhausted_id)
    exhausted.queued_at = long_ago
    exhausted.attempts = document_service.MAX_ATTEMPTS
    db_session.commit()
    sent.clear()  # 清掉上傳時送的工單，只看這次掃描送了什麼

    result = worker.handle_sweep(None, None)

    assert result == {"requeued": 1, "orphans_deleted": 0}
    assert sent == [stuck_id]
    assert db_session.get(Document, fresh_id).status == "uploaded"
    assert db_session.get(Document, exhausted_id).status == "failed"


# ---------- docs/failure-modes.md 的缺口 3：程式有處理、但原本沒有測試的情況 ----------


# 測試：送工單前「記錄送出時間」那步就失敗了（queued_at 是空的）→ sweeper 改看上傳時間，一樣會重送
def test_sweep_requeues_document_without_queued_at(client, db_session, sent, worker_db):
    token = _register_and_login(client, "kate@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.queued_at = None
    document.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()
    sent.clear()

    worker.handle_sweep(None, None)

    assert sent == [document_id]


# 測試：同一份文件的工單來了兩次（SQS 只保證「至少送一次」）→ 第二次直接跳過，不重複處理
def test_worker_skips_duplicate_message(client, db_session, monkeypatch, sent, worker_db):
    token = _register_and_login(client, "leo@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_pdf(client, token, ws_id).json()["id"]
    worker.handle_process_message(_sqs_event(document_id), None)
    calls = []
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts: calls.append(texts))

    worker.handle_process_message(_sqs_event(document_id), None)

    assert calls == []  # 第二次完全沒有呼叫 OpenAI
    assert db_session.get(Document, document_id).status == "processed"
