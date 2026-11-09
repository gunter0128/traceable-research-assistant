# 背景處理（雲端模式）的測試：送工單、Lambda 收工單、手動重試、定期掃描
# 真的 SQS 換成一個記錄「送了哪些工單」的 list，跟 conftest.py 的 fake_s3 用 dict 假裝 S3 是同一個道理

import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import EndpointConnectionError

from app import worker
from app.core import openai_client, sqs_client
from app.core.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import document_service
from tests.helpers import create_workspace, fake_embed_texts, login_headers, upload_pdf
from tests.pdf_fixtures import valid_pdf_bytes


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)


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


def _upload_pdf(client, headers, workspace_id):
    return upload_pdf(client, headers, workspace_id, valid_pdf_bytes())


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
    headers = login_headers(client, "alice@example.com")
    ws_id = create_workspace(client, headers)

    response = _upload_pdf(client, headers, ws_id)

    body = response.json()
    assert response.status_code == 201
    assert body["status"] == "uploaded"
    assert sent == [body["id"]]
    assert _chunk_count(db_session, body["id"]) == 0


# 測試：工單送不出去 → 文件直接標失敗，不會永遠卡在處理中
def test_upload_marks_failed_when_send_fails(client, monkeypatch, sent):
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    headers = login_headers(client, "bob@example.com")
    ws_id = create_workspace(client, headers)

    response = _upload_pdf(client, headers, ws_id)

    assert response.json()["status"] == "failed"
    assert response.json()["error_message"] == "排入處理佇列失敗，可以重試"


# 測試：Lambda 收到工單 → 文件被處理完成，chunk 存進去
def test_worker_processes_document(client, db_session, sent, worker_db):
    headers = login_headers(client, "carol@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]

    worker.handle_process_message(_sqs_event(document_id), None)

    document = db_session.get(Document, document_id)
    db_session.refresh(document)
    assert document.status == "processed"
    assert _chunk_count(db_session, document_id) > 0


# 測試：工單送出後文件被刪掉了 → Lambda 不會出錯，直接跳過
def test_worker_skips_deleted_document(client, sent, worker_db):
    headers = login_headers(client, "dave@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    client.delete(f"/documents/{document_id}", headers=headers)

    worker.handle_process_message(_sqs_event(document_id), None)  # 不丟例外就算通過


# 測試：次數用完的文件，就算又收到工單，也不會再處理，直接標失敗
# 自動延後重試拿掉之後（docs/decisions/046），次數只會因為 SQS 重送工單、接手死掉的 Lambda 而增加；
# 這個上限是「一份會讓程式當掉的文件不會被一直重做」的保險。邊界（第 3 次照常處理）見
# test_openai_errors.py 的 test_temporary_error_on_last_allowed_attempt_fails_normally
def test_process_stops_after_max_attempts(client, db_session, monkeypatch):
    headers = login_headers(client, "erin@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.status = "uploaded"
    document.attempts = document_service.MAX_ATTEMPTS
    db_session.commit()

    # 從這裡開始記錄有沒有呼叫 OpenAI。不在假函式裡直接 raise：
    # process_document 會接住所有錯誤、把文件標失敗，測試反而看不出「其實呼叫了」
    calls = []

    def record_embed(texts, **kwargs):
        calls.append(texts)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", record_embed)
    document_service.process_document(db_session, document)

    assert document.attempts == document_service.MAX_ATTEMPTS + 1
    assert document.status == "failed"
    assert document.error_message == "處理了 3 次都沒有成功，已停止自動重試"  # 是次數上限擋下來的，不是別的錯誤
    assert calls == []  # 完全沒有呼叫 OpenAI


# 測試：失敗的文件可以手動重試，次數歸零並重新送工單
def test_retry_failed_document(client, db_session, monkeypatch, sent):
    headers = login_headers(client, "frank@example.com")
    ws_id = create_workspace(client, headers)
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]  # 送不出去 → failed
    monkeypatch.setattr(sqs_client, "send_process_message", sent.append)

    response = client.post(f"/documents/{document_id}/retry", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    assert sent == [document_id]
    assert db_session.get(Document, document_id).attempts == 0
    assert response.json()["error_message"] is None  # 舊的失敗原因被清掉了


# 測試：不是失敗狀態的文件不能重試（409）
def test_cannot_retry_non_failed_document(client, sent):
    headers = login_headers(client, "grace@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]  # 處理中

    response = client.post(f"/documents/{document_id}/retry", headers=headers)

    assert response.status_code == 409


# 測試：不能重試別人的文件（403）
def test_cannot_retry_others_document(client, sent, monkeypatch):
    owner_headers = login_headers(client, "henry@example.com")
    ws_id = create_workspace(client, owner_headers)
    monkeypatch.setattr(sqs_client, "send_process_message", _broken_send)
    document_id = _upload_pdf(client, owner_headers, ws_id).json()["id"]
    other_headers = login_headers(client, "ivy@example.com")

    response = client.post(f"/documents/{document_id}/retry", headers=other_headers)

    assert response.status_code == 403


# 測試：sweeper → 交出去超過 15 分鐘還在處理中的標「處理逾時」；剛送出的不動；不會再重送工單
def test_sweep_expires_only_overdue_documents(client, db_session, sent, worker_db):
    headers = login_headers(client, "jack@example.com")
    ws_id = create_workspace(client, headers)
    overdue_id = _upload_pdf(client, headers, ws_id).json()["id"]
    fresh_id = _upload_pdf(client, headers, ws_id).json()["id"]
    db_session.get(Document, overdue_id).queued_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db_session.commit()
    sent.clear()  # 清掉上傳時送的工單，只看這次掃描有沒有送東西

    result = worker.handle_sweep(None, None)

    assert result == {"expired": 1, "orphans_deleted": 0}
    assert sent == []
    overdue = db_session.get(Document, overdue_id)
    assert overdue.status == "failed"
    assert overdue.error_message == "處理逾時，請按重試"
    assert db_session.get(Document, fresh_id).status == "uploaded"


# ---------- docs/failure-modes.md 的缺口 3：程式有處理、但原本沒有測試的情況 ----------


# 測試：送工單前「記錄送出時間」那步就失敗了（queued_at 是空的）→ 改看上傳時間，一樣會被標逾時
def test_sweep_expires_document_without_queued_at(client, db_session, sent, worker_db):
    headers = login_headers(client, "kate@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.queued_at = None
    document.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    worker.handle_sweep(None, None)

    assert db_session.get(Document, document_id).status == "failed"


# ---------- docs/decisions/007：演練 3 發現程式連「次數 +1」都做不到時，文件會永遠卡在處理中 ----------


# 測試：使用者查看文件列表時就檢查期限，不用等每小時的 sweeper
def test_listing_expires_overdue_document(client, db_session, sent):
    headers = login_headers(client, "mia@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.queued_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db_session.commit()

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()

    assert documents[0]["status"] == "failed"
    assert documents[0]["error_message"] == "處理逾時，請按重試"


# 測試：處理中但還沒超過期限 → 不動（不能把正常處理中的文件誤判成逾時）
def test_listing_keeps_document_within_deadline(client, db_session, sent):
    headers = login_headers(client, "nina@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.queued_at = datetime.now(timezone.utc) - timedelta(minutes=14)
    db_session.commit()

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()

    assert documents[0]["status"] == "uploaded"


# 測試：逾時的文件可以按重試，重試後重新計算 15 分鐘（不會因為上傳時間很久以前就馬上又逾時）
def test_overdue_document_can_be_retried(client, db_session, sent):
    headers = login_headers(client, "owen@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    document.uploaded_at = datetime.now(timezone.utc) - timedelta(hours=2)
    document.queued_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()

    response = client.post(f"/documents/{document_id}/retry", headers=headers)
    after = client.get(f"/documents/{document_id}", headers=headers).json()

    assert response.status_code == 200
    assert after["status"] == "uploaded"


# 測試：同一份文件的工單來了兩次（SQS 只保證「至少送一次」）→ 第二次直接跳過，不重複處理
def test_worker_skips_duplicate_message(client, db_session, monkeypatch, sent, worker_db):
    headers = login_headers(client, "leo@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_pdf(client, headers, ws_id).json()["id"]
    worker.handle_process_message(_sqs_event(document_id), None)

    # 從這裡開始記錄有沒有呼叫 OpenAI（理由同 test_process_stops_after_max_attempts）
    calls = []

    def record_embed(texts, **kwargs):
        calls.append(texts)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", record_embed)

    worker.handle_process_message(_sqs_event(document_id), None)

    assert calls == []  # 第二次完全沒有呼叫 OpenAI
    assert db_session.get(Document, document_id).status == "processed"
