# 處理文件時的交易邊界、用量、心跳（docs/decisions/046）
# 測的是：
#   下載 PDF、呼叫 OpenAI 的時候，主 session 沒有開著的交易
#   每轉完一批，用量就已經 commit；這時候 chunks 一段都還沒存（用量的 commit 不會把 chunks 帶出去）
#   後面的批次失敗：已經花掉的用量留著，chunks 一段都不存
#   記用量失敗：不再轉下一批，留下告警看得到的 ERROR
#   最後一次報平安之後租約才被收回：存結果那一步擋下來，一段 chunk 都不留
#   報平安時資料庫出錯：標失敗，不存 chunks
#   已經處理完的文件：sweeper、使用者查看都不會改到它
#   心跳的期限比「一批轉向量加上它所有的重試」最久要花的時間還長

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError

from app.core import openai_client, s3_client
from app.core.db import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.usage_record import UsageRecord
from app.repositories import document_repo
from app.services import document_service, usage_service
from tests.helpers import (
    always_raise,
    bucket,
    create_workspace,
    fake_embed_texts,
    login_headers,
    upload_pdf,
)
from tests.pdf_fixtures import valid_pdf_bytes

# 夠長的文字，切下去會變成好幾段；搭配一次只送一段去轉向量，才會有「第 2 批」
LONG_TEXT = "retrieval " * 2000


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)


# 建一份排隊中的文件，檔案放進假的 S3
# 不透過上傳的 API：本機模式上傳完會直接處理掉，這裡要自己呼叫 process_document，才能在中間檢查
def _queued_document(client, db_session, email, text=LONG_TEXT):
    headers = login_headers(client, email)
    ws_id = create_workspace(client, headers)
    key = f"documents/{ws_id}/{email}.pdf"
    bucket[key] = valid_pdf_bytes(text)
    return document_repo.create(db_session, ws_id, "doc.pdf", key)


# 另開一個 session 數：這份文件現在資料庫裡有幾段 chunk、幾筆用量
# 另一個 session 只看得到已經 commit 的，還在交易裡、沒 commit 的看不到
def _committed_counts(document_id):
    with SessionLocal() as other:
        chunks = other.query(Chunk).filter(Chunk.document_id == document_id).count()
        usages = other.query(UsageRecord).filter(UsageRecord.document_id == document_id).count()
    return chunks, usages


# 測試：下載 PDF、呼叫 OpenAI 的時候，主 session（db）都沒有開著的交易
def test_no_transaction_during_s3_and_openai_calls(client, db_session, monkeypatch):
    document = _queued_document(client, db_session, "alice@example.com")
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    # 每次呼叫外部服務時，主 session 有沒有開著交易：[("S3", False), ("OpenAI", False), ...]
    in_transaction = []

    def download(key):
        in_transaction.append(("S3", db_session.in_transaction()))
        return bucket[key]

    def embed(texts):
        in_transaction.append(("OpenAI", db_session.in_transaction()))
        return fake_embed_texts(texts)

    monkeypatch.setattr(s3_client, "download_file", download)
    monkeypatch.setattr(openai_client, "embed_texts", embed)

    document_service.process_document(db_session, document)

    assert document.status == "processed"
    assert len(in_transaction) > 2  # 下載一次、轉向量好幾批，確認真的有檢查到
    for call, was_in_transaction in in_transaction:
        assert was_in_transaction is False, call


# 測試：每轉完一批，用量就已經 commit 進資料庫（另一個 session 看得到）；這時候 chunks 一段都還沒存
# 也就是說，用量的 commit 不會把 chunks 提早帶出去；全部轉完、存結果的時候，chunks 才一次出現
def test_usage_committed_after_each_batch_but_chunks_only_at_the_end(client, db_session, monkeypatch):
    document = _queued_document(client, db_session, "bob@example.com")
    document_id = document.id  # 先記下來：document 的欄位之後會過期，在假函式裡讀會讓主 session 又開一個交易
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    # 每一批轉向量之前，資料庫裡已經 commit 的 (chunk 段數, 用量筆數)
    seen = []

    def embed(texts):
        seen.append(_committed_counts(document_id))
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", embed)

    document_service.process_document(db_session, document)

    batches = len(seen)
    assert batches > 2
    # 第 n 批之前：前面 n - 1 批的用量都已經存了，chunks 還是 0
    assert seen == [(0, done) for done in range(batches)]
    # 全部做完：每批一段 chunk、每批一筆用量
    assert _committed_counts(document_id) == (batches, batches)
    assert document.status == "processed"


# 測試：第 2 批轉向量失敗 → 第 1 批的用量留著（錢已經花了），chunks 一段都沒有，文件標失敗
def test_failed_second_batch_keeps_usage_and_saves_no_chunks(client, db_session, monkeypatch):
    document = _queued_document(client, db_session, "carol@example.com")
    document_id = document.id
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    calls = []

    def fail_on_second_batch(texts):
        calls.append(texts)
        if len(calls) == 2:
            raise openai_client.OpenAITemporaryError()
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", fail_on_second_batch)

    document_service.process_document(db_session, document)

    assert len(calls) == 2
    assert document.status == "failed"
    assert document.error_message == "AI 服務暫時無法使用，請稍後按重試"
    assert _committed_counts(document_id) == (0, 1)  # 0 段 chunk、1 筆用量（第 1 批的）


# 測試：第 1 批轉完、記用量的時候資料庫出錯 → 這一批的錢花了但沒記到（每日上限會少算這一批），所以：
#   不再轉下一批（不再花錢）、文件標失敗、chunks 一段都不存
#   留下 ERROR 的 log、附上錯誤堆疊：告警看得到，事後查得到是記用量失敗
def test_usage_record_failure_stops_embedding_and_logs_error(client, db_session, monkeypatch, logs):
    document = _queued_document(client, db_session, "dora@example.com")
    document_id = document.id
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    calls = []

    def record_embed(texts):
        calls.append(texts)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", record_embed)
    # OperationalError：SQLAlchemy 在「連線斷了、資料庫沒回應」這類情況丟的錯誤
    connection_lost = OperationalError("INSERT INTO usage_records ...", {}, Exception("server closed the connection"))
    monkeypatch.setattr(usage_service, "record", always_raise(connection_lost))

    document_service.process_document(db_session, document)

    assert len(calls) == 1  # 只轉了第 1 批
    assert document.status == "failed"
    assert document.error_message == "處理時發生錯誤，可以稍後重試"
    assert _committed_counts(document_id) == (0, 0)
    [error] = logs.errors()
    assert error["event"] == "unexpected_error"
    assert "OperationalError" in error["traceback"]


# 測試：最後一次報平安之後、存結果之前，租約才被收回（例如 sweeper 剛好把它判定中斷）
# → 存結果那一步的有條件 UPDATE 改不到，整批放棄：chunks 一段都不留，文件維持被判定的狀態
def test_lease_lost_after_last_heartbeat_saves_no_chunks(client, db_session, monkeypatch):
    document = _queued_document(client, db_session, "dave@example.com", text="short text")  # 只有 1 段、1 批
    document_id = document.id
    real_update_heartbeat = document_repo.update_heartbeat
    heartbeat_results = []

    # 報平安照常（真的去更新）；第 2 次（轉完唯一那一批之後）報完，模擬 sweeper 把它判定中斷
    def heartbeat_then_expire(db, doc_id, token):
        result = real_update_heartbeat(db, doc_id, token)
        heartbeat_results.append(result)
        if len(heartbeat_results) == 2:
            with SessionLocal() as other:
                other.query(Document).filter(Document.id == doc_id).update(
                    {Document.status: "failed", Document.error_message: "處理中斷，請按重試", Document.claim_token: None}
                )
                other.commit()
        return result

    monkeypatch.setattr(document_repo, "update_heartbeat", heartbeat_then_expire)

    document_service.process_document(db_session, document)

    assert heartbeat_results == [True, True]  # 報平安都成功：是存結果那一步擋下來的
    assert document.status == "failed"
    assert document.error_message == "處理中斷，請按重試"
    assert _committed_counts(document_id) == (0, 1)  # 用量照記，chunks 一段都沒有


# 測試：報平安的時候資料庫出錯 → 文件標失敗，chunks 一段都不存
def test_heartbeat_error_fails_document_without_saving_chunks(client, db_session, monkeypatch):
    document = _queued_document(client, db_session, "erin@example.com")
    document_id = document.id

    def broken_heartbeat(db, doc_id, token):
        raise RuntimeError("模擬資料庫斷線")

    monkeypatch.setattr(document_repo, "update_heartbeat", broken_heartbeat)

    document_service.process_document(db_session, document)

    assert document.status == "failed"
    assert document.error_message == "處理時發生錯誤，可以稍後重試"
    assert _committed_counts(document_id)[0] == 0


# 測試：已經處理完的文件，就算報平安、送出工單、上傳的時間都是很久以前，sweeper 和使用者查看都不會改到它
def test_sweeper_and_listing_do_not_touch_processed_documents(client, db_session):
    headers = login_headers(client, "frank@example.com")
    ws_id = create_workspace(client, headers)
    document_id = upload_pdf(client, headers, ws_id, valid_pdf_bytes()).json()["id"]  # 本機模式：上傳完就處理好了
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    document = db_session.get(Document, document_id)
    document.heartbeat_at = two_hours_ago
    document.queued_at = two_hours_ago
    document.uploaded_at = two_hours_ago
    db_session.commit()
    chunks_before, _ = _committed_counts(document_id)

    expired = document_service.expire_overdue_documents(db_session)
    listed = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()

    assert expired == 0
    assert listed[0]["status"] == "processed"
    assert chunks_before > 0
    assert _committed_counts(document_id)[0] == chunks_before


# 測試：心跳的期限，比「一批轉向量加上它所有的重試」最久要花的時間還長（docs/decisions/046）
# 改了 openai_client 的重試設定（次數、每次等多久、OpenAI 叫我們等的上限）卻忘了調 HEARTBEAT_TIMEOUT，這裡會擋下來
# 重試之間不是照 OpenAI 說的等（最多 PROCESSING_MAX_WAIT），就是自己算的間隔（最多 8 秒），所以用 PROCESSING_MAX_WAIT 算最壞情況
def test_heartbeat_timeout_covers_one_batch_with_all_retries():
    attempts = openai_client.PROCESSING_ATTEMPTS
    longest_batch_seconds = (
        attempts * openai_client.PROCESSING_TIMEOUT  # 每次嘗試最多等這麼久
        + (attempts - 1) * openai_client.PROCESSING_MAX_WAIT  # 兩次嘗試之間最多等這麼久
    )

    assert document_service.HEARTBEAT_TIMEOUT.total_seconds() > longest_batch_seconds
