# 租約、心跳（docs/decisions/008、046）
# 測的是：
#   只有排隊中的文件搶得到租約，同一份文件同時只有一個 Lambda 能處理（包括真的同時搶）
#   處理中的文件不會被別的 Lambda 接手：超過 10 分鐘沒報平安，由 sweeper / 使用者查看時標成「處理中斷」，再由使用者按重試
#   租約已經不是自己的了（被判定中斷、使用者取消、文件被刪掉）的 Lambda 會停下來，結果存不進去
#   處理到一半文件被刪掉：worker 正常結束（不算當掉、不寫 ERROR），本機模式上傳的請求回 404

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import worker
from app.core import openai_client
from app.core.db import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.repositories import document_repo
from app.services import document_service
from tests.helpers import create_workspace, fake_embed_texts, login_headers, upload_pdf
from tests.pdf_fixtures import valid_pdf_bytes

# 夠長的文字，切下去會變成好幾段
LONG_TEXT = "retrieval " * 2000


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)


# 登入 + 建 workspace + 上傳一份（本機模式，會直接處理完），回傳 headers、workspace、文件編號
def _upload(client, email, text=LONG_TEXT):
    headers = login_headers(client, email)
    ws_id = create_workspace(client, headers)
    response = upload_pdf(client, headers, ws_id, valid_pdf_bytes(text))
    return headers, ws_id, response.json()["id"]


def _set(db_session, document_id, **fields):
    document = db_session.get(Document, document_id)
    for name, value in fields.items():
        setattr(document, name, value)
    db_session.commit()
    return document


def _minutes_ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# 在新的 session 裡搶租約：模擬另一個 Lambda（每個 Lambda 有自己的資料庫連線）
# barrier：兩條執行緒都走到這裡才一起往下，讓兩個 UPDATE 盡量在同一時間送到資料庫
def _claim_in_new_session(document_id, barrier):
    with SessionLocal() as db:
        barrier.wait()
        return document_repo.claim(db, document_id, uuid4().hex)


# 用另一個 session 刪掉文件：模擬使用者在 API 那邊按了刪除（API 和 worker 是不同的 Lambda，資料庫連線也不同）
# 走真的 delete_document：先刪紀錄、再刪 S3 的檔案，跟使用者按刪除時一樣
def _delete_from_other_session(document_id):
    with SessionLocal() as api_db:
        owner_id = api_db.get(Document, document_id).workspace.owner_id
        document_service.delete_document(api_db, document_id, owner_id)


# ---------- 取得租約 ----------


# 測試：同一份文件的租約搶兩次 → 只有第一次成功
def test_claim_is_exclusive(client, db_session):
    _, _, document_id = _upload(client, "bob@example.com")
    _set(db_session, document_id, status="uploaded")

    first = document_repo.claim(db_session, document_id, uuid4().hex)
    second = document_repo.claim(db_session, document_id, uuid4().hex)

    assert first is True
    assert second is False


# 測試：兩個 Lambda 真的同時搶（兩條執行緒、兩條資料庫連線）→ 只有一個成功
# 資料庫怎麼保證：先到的 UPDATE 鎖住這一列，後到的等它 commit 之後重新檢查條件，狀態已經是處理中，改不到
def test_concurrent_claims_only_one_succeeds(client, db_session):
    _, _, document_id = _upload(client, "bella@example.com")
    _set(db_session, document_id, status="uploaded")
    # timeout：其中一條執行緒出錯、走不到 barrier 的話，另一條等 10 秒就放棄，測試不會卡住
    barrier = threading.Barrier(2, timeout=10)

    # ThreadPoolExecutor：開兩條執行緒同時跑 _claim_in_new_session
    # future.result()：等那條執行緒跑完、拿它的回傳值；執行緒裡出錯的話，錯誤會在這裡丟出來
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_claim_in_new_session, document_id, barrier) for _ in range(2)]
        results = [future.result() for future in futures]

    assert sorted(results) == [False, True]


# 測試：不是排隊中的文件都搶不到租約，原本的狀態和租約代碼完全不動（docs/decisions/046）
#   處理中：不管上一個 Lambda 多久沒報平安都不接手
#     11 分鐘：已經超過 10 分鐘的期限（以前會被接手的情況）；9 分鐘：還在期限內
#   已完成、已失敗、已取消：重複的工單，不用再處理
@pytest.mark.parametrize(
    ("status", "minutes_since_heartbeat"),
    [("processing", 11), ("processing", 9), ("processed", 0), ("failed", 0), ("cancelled", 0)],
)
def test_only_uploaded_document_can_be_claimed(client, db_session, status, minutes_since_heartbeat):
    _, _, document_id = _upload(client, "carol@example.com")
    _set(db_session, document_id, status=status, claim_token="old", heartbeat_at=_minutes_ago(minutes_since_heartbeat))

    assert document_repo.claim(db_session, document_id, "new") is False
    document = db_session.get(Document, document_id)
    assert document.status == status
    assert document.claim_token == "old"


# 測試：處理中的 Lambda 死掉了（11 分鐘沒報平安）的完整經過：
#   1. SQS 重送的工單進來 → 搶不到租約，不接手、不處理
#   2. 使用者查看 → 標成「處理中斷，請按重試」
#   3. 使用者按重試 → 回到排隊中、重新處理完成（本機模式，按重試就直接處理）
def test_interrupted_document_is_not_taken_over_but_can_be_retried(client, db_session):
    headers, _, document_id = _upload(client, "dave@example.com")
    _set(db_session, document_id, status="processing", claim_token="dead", heartbeat_at=_minutes_ago(11))
    db_session.query(Chunk).filter(Chunk.document_id == document_id).delete()  # 上傳時處理出來的先清掉，最後才看得出有沒有重新處理
    db_session.commit()

    skipped = document_service.process_document(db_session, db_session.get(Document, document_id))
    assert skipped.status == "processing"
    assert skipped.claim_token == "dead"

    viewed = client.get(f"/documents/{document_id}", headers=headers).json()
    assert viewed["status"] == "failed"
    assert viewed["error_message"] == "處理中斷，請按重試"

    retried = client.post(f"/documents/{document_id}/retry", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["status"] == "processed"
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() > 0


# ---------- 心跳、判定中斷 ----------


# 測試：使用者查看時，處理中超過 10 分鐘沒報平安 → 「處理中斷，請按重試」
def test_listing_marks_silent_document_interrupted(client, db_session):
    headers, ws_id, document_id = _upload(client, "erin@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(11))

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()

    assert documents[0]["status"] == "failed"
    assert documents[0]["error_message"] == "處理中斷，請按重試"


# 測試：處理中、9 分鐘前才報過平安 → 不動（不能把還活著的誤判成中斷）
def test_listing_keeps_document_with_recent_heartbeat(client, db_session):
    headers, ws_id, document_id = _upload(client, "frank@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(9))

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()

    assert documents[0]["status"] == "processing"


# 測試：每小時的 sweeper 也會把沒報平安的標中斷
def test_sweep_marks_silent_document_interrupted(client, db_session):
    _, _, document_id = _upload(client, "grace@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(11))

    assert document_service.expire_overdue_documents(db_session) == 1
    db_session.refresh(db_session.get(Document, document_id))
    assert db_session.get(Document, document_id).error_message == "處理中斷，請按重試"


# 測試：很慢的 Lambda 被誤判中斷，使用者按了重試、新的 Lambda 已經開始處理 → 慢的那個轉完這一批、報平安時發現
# 租約不是自己的了，放棄這次處理：chunk 一段都不留，也不碰文件狀態（現在是新的 Lambda 的）
def test_slow_worker_gives_up_after_retry_started_new_worker(client, db_session, monkeypatch):
    _, _, document_id = _upload(client, "henry@example.com")
    document = _set(db_session, document_id, status="uploaded")
    db_session.query(Chunk).filter(Chunk.document_id == document_id).delete()
    db_session.commit()

    # 假的 OpenAI：轉這一批的期間，在別的 session 照順序發生三件事（都走真的函式）
    def judged_interrupted_and_retried(texts):
        with SessionLocal() as other:
            # 1. 被判定中斷：把最後報平安的時間改到 11 分鐘前，再跑一次逾時檢查（跟 sweeper 做的一樣）
            other.query(Document).filter(Document.id == document_id).update({Document.heartbeat_at: _minutes_ago(11)})
            other.commit()
            document_service.expire_overdue_documents(other, document_id=document_id)
            # 2. 使用者按重試  3. 新的 Lambda 取得租約
            document_repo.reset_for_retry(other, document_id)
            document_repo.claim(other, document_id, "new-worker")
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", judged_interrupted_and_retried)

    document_service.process_document(db_session, document)

    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0
    db_session.refresh(document)
    assert document.status == "processing"
    assert document.claim_token == "new-worker"


# 測試：租約不是自己的了，最後「存結果」也會被擋下來
def test_finish_rejected_with_wrong_token(client, db_session):
    _, _, document_id = _upload(client, "ivy@example.com")
    _set(db_session, document_id, status="processing", claim_token="real", heartbeat_at=datetime.now(timezone.utc))

    assert document_repo.mark_processed(db_session, document_id, "fake", "hash") is False
    db_session.commit()  # 就算 commit 了也沒有東西被改到
    db_session.refresh(db_session.get(Document, document_id))
    assert db_session.get(Document, document_id).status == "processing"


# 測試：被判定中斷之後，那個其實只是很慢的 Lambda 再來報平安 → 失敗（租約已經收回），不會把文件「救活」
def test_heartbeat_rejected_after_interrupted(client, db_session):
    headers, ws_id, document_id = _upload(client, "jack@example.com")
    _set(db_session, document_id, status="processing", claim_token="slow", heartbeat_at=_minutes_ago(11))
    client.get(f"/workspaces/{ws_id}/documents", headers=headers)  # 觸發判定中斷

    assert document_repo.update_heartbeat(db_session, document_id, "slow") is False
    db_session.refresh(db_session.get(Document, document_id))
    assert db_session.get(Document, document_id).status == "failed"


# ---------- 處理到一半，文件被刪掉 ----------
# 文件被刪掉 = 租約也跟著沒了：報平安、存結果、標失敗都改不到任何一列，worker 停下來
# 停下來之後回傳「資料庫裡最新的樣子」，被刪掉了就是 None（不能 db.refresh：讀不到已經刪掉的文件會丟出錯誤）


# 測試：worker 轉向量的期間，使用者按了刪除 → 這批轉完、報平安時發現，停下來
# worker 正常結束：不丟例外（SQS 不會重送）、不寫 ERROR（不會觸發告警）
def test_worker_stops_quietly_when_document_deleted_while_embedding(client, db_session, monkeypatch, logs):
    _, _, document_id = _upload(client, "quinn@example.com")
    _set(db_session, document_id, status="uploaded")
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)  # worker 自己開的 session 換成測試用的

    def delete_then_embed(texts):
        _delete_from_other_session(document_id)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", delete_then_embed)
    event = {"Records": [{"body": json.dumps({"document_id": document_id})}]}

    worker.handle_process_message(event, None)  # 不丟例外就代表這次執行成功

    assert logs.named("lease_lost")[0]["status"] == "deleted"
    assert logs.errors() == []
    assert db_session.get(Document, document_id) is None
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0


# 測試：最後一次報平安之後、存結果之前，文件被刪掉 → 存結果那一步改不到，整批放棄，回傳 None
def test_document_deleted_just_before_saving(client, db_session, monkeypatch, logs):
    _, _, document_id = _upload(client, "rita@example.com")
    document = _set(db_session, document_id, status="uploaded")
    real_mark_processed = document_repo.mark_processed

    def delete_then_mark_processed(db, doc_id, token, content_hash):
        _delete_from_other_session(doc_id)
        return real_mark_processed(db, doc_id, token, content_hash)

    monkeypatch.setattr(document_repo, "mark_processed", delete_then_mark_processed)

    result = document_service.process_document(db_session, document)

    assert result is None
    assert logs.named("lease_lost")[0]["status"] == "deleted"
    assert logs.errors() == []
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0


# 測試：文件被刪掉之後，處理又剛好出錯（OpenAI 暫時連不上）→ 標失敗那一步改不到任何東西，回傳 None
def test_document_deleted_then_processing_fails(client, db_session, monkeypatch, logs):
    _, _, document_id = _upload(client, "sam@example.com")
    document = _set(db_session, document_id, status="uploaded")

    def delete_then_fail(texts):
        _delete_from_other_session(document_id)
        raise openai_client.OpenAITemporaryError()

    monkeypatch.setattr(openai_client, "embed_texts", delete_then_fail)

    result = document_service.process_document(db_session, document)

    assert result is None
    assert logs.named("failed")[0]["error_type"] == "OpenAITemporaryError"
    assert logs.errors() == []


# 測試：本機模式上傳完直接處理，處理到一半文件被刪掉（例如在別的分頁按了刪除）→ 上傳的請求回 404，不是 500
def test_inline_upload_returns_404_when_document_deleted_while_processing(client, monkeypatch):
    headers = login_headers(client, "tina@example.com")
    ws_id = create_workspace(client, headers)

    # 上傳的請求還沒回來，拿不到文件編號：從資料庫找這個 workspace 唯一的那份
    def delete_then_embed(texts):
        with SessionLocal() as other:
            (document_id,) = other.query(Document.id).filter(Document.workspace_id == ws_id).one()
        _delete_from_other_session(document_id)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", delete_then_embed)

    response = upload_pdf(client, headers, ws_id, valid_pdf_bytes())

    assert response.status_code == 404


# ---------- 取消處理（docs/decisions/009）----------


# 測試：Lambda 正在處理時，使用者按了取消 → Lambda 下次報平安時發現代碼對不上，自己停下來，
# 寫到一半的 chunk 一段都不留，文件狀態是「已取消」
def test_cancel_stops_running_worker(client, db_session, monkeypatch):
    headers, _, document_id = _upload(client, "kate@example.com")
    document = _set(db_session, document_id, status="uploaded")
    db_session.query(Chunk).filter(Chunk.document_id == document_id).delete()
    db_session.commit()

    # 假的 OpenAI：被呼叫時，模擬「使用者在這時候按了取消」（走真的 API，跟使用者做的事一樣）
    def cancel_then_embed(texts, **kwargs):
        response = client.post(f"/documents/{document_id}/cancel", headers=headers)
        assert response.status_code == 200
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", cancel_then_embed)

    document_service.process_document(db_session, document)

    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0
    db_session.refresh(document)
    assert document.status == "cancelled"


# 測試：還在排隊時取消 → 之後 Lambda 拿到工單，搶不到租約，完全不處理
def test_cancelled_document_is_skipped_by_worker(client, db_session, monkeypatch):
    headers, _, document_id = _upload(client, "leo@example.com")
    document = _set(db_session, document_id, status="uploaded")
    client.post(f"/documents/{document_id}/cancel", headers=headers)

    # 從這裡開始記錄有沒有呼叫 OpenAI。不在假函式裡直接 raise：
    # process_document 會接住所有錯誤，測試反而看不出「其實呼叫了」
    calls = []

    def record_embed(texts, **kwargs):
        calls.append(texts)
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", record_embed)

    document_service.process_document(db_session, document)

    assert calls == []
    db_session.refresh(document)
    assert document.status == "cancelled"


# 測試：已經處理完的文件不能取消（409），沒有東西可以停
def test_cannot_cancel_processed_document(client):
    headers, _, document_id = _upload(client, "mia@example.com")  # 本機模式，上傳完就處理好了

    response = client.post(f"/documents/{document_id}/cancel", headers=headers)

    assert response.status_code == 409


# 測試：不能取消別人的文件（403）
def test_cannot_cancel_others_document(client, db_session):
    _, _, document_id = _upload(client, "nina@example.com")
    _set(db_session, document_id, status="uploaded")
    other_headers = login_headers(client, "owen@example.com")

    response = client.post(f"/documents/{document_id}/cancel", headers=other_headers)

    assert response.status_code == 403


# 測試：取消之後可以按重試，重新處理完成
def test_cancelled_document_can_be_retried(client, db_session):
    headers, _, document_id = _upload(client, "paul@example.com")
    _set(db_session, document_id, status="uploaded")
    client.post(f"/documents/{document_id}/cancel", headers=headers)

    response = client.post(f"/documents/{document_id}/retry", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
