# 租約、心跳、進度（docs/decisions/008）
# 測的是：同一份文件同時只有一個 Lambda 能處理；處理中一直報平安；停止報平安就判定中斷；
# 租約被搶走的那個 Lambda 會放棄，不會跟接手的存出兩份內容

import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core import openai_client, s3_client
from app.core.db import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.repositories import document_repo
from app.services import document_service
from tests.pdf_fixtures import valid_pdf_bytes

# 夠長的文字，切下去會變成好幾段，進度才看得出來
LONG_TEXT = "retrieval " * 2000


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts, **kwargs: [[0.1] * 1536 for _ in texts])


def _register_and_login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    response = client.post("/auth/login", json={"email": email, "password": "pw123456"})
    return response.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# 上傳一份（本機模式，會直接處理完），回傳 token、workspace、文件編號
def _upload(client, email, text=LONG_TEXT):
    token = _register_and_login(client, email)
    ws_id = client.post("/workspaces", json={"name": "研究"}, headers=_auth_headers(token)).json()["id"]
    response = client.post(
        f"/workspaces/{ws_id}/documents",
        headers=_auth_headers(token),
        files={"file": ("doc.pdf", io.BytesIO(valid_pdf_bytes(text)), "application/pdf")},
    )
    return token, ws_id, response.json()["id"]


def _set(db_session, document_id, **fields):
    document = db_session.get(Document, document_id)
    for name, value in fields.items():
        setattr(document, name, value)
    db_session.commit()
    return document


def _minutes_ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# 測試：處理完之後，進度是「全部段數 / 全部段數」
def test_progress_recorded(client, db_session):
    _, _, document_id = _upload(client, "alice@example.com")

    document = db_session.get(Document, document_id)
    chunk_count = db_session.query(Chunk).filter(Chunk.document_id == document_id).count()
    assert document.status == "processed"
    assert document.progress_total == chunk_count > 1
    assert document.progress_done == chunk_count


# 測試：兩個 Lambda 同時搶同一份文件的租約 → 只有一個成功
def test_claim_is_exclusive(client, db_session):
    _, _, document_id = _upload(client, "bob@example.com")
    _set(db_session, document_id, status="uploaded")
    stale_before = _minutes_ago(5)

    first = document_repo.claim(db_session, document_id, uuid4().hex, stale_before)
    second = document_repo.claim(db_session, document_id, uuid4().hex, stale_before)

    assert first is True
    assert second is False


# 測試：處理中、但上一個 Lambda 已經超過 5 分鐘沒報平安（判定它死了）→ 可以接手
def test_stale_lease_can_be_taken_over(client, db_session):
    _, _, document_id = _upload(client, "carol@example.com")
    _set(db_session, document_id, status="processing", claim_token="old", heartbeat_at=_minutes_ago(6))

    assert document_repo.claim(db_session, document_id, "new", _minutes_ago(5)) is True


# 測試：處理中、上一個 Lambda 4 分鐘前才報過平安（還活著）→ 不能搶
def test_fresh_lease_cannot_be_taken_over(client, db_session):
    _, _, document_id = _upload(client, "dave@example.com")
    _set(db_session, document_id, status="processing", claim_token="old", heartbeat_at=_minutes_ago(4))

    assert document_repo.claim(db_session, document_id, "new", _minutes_ago(5)) is False


# 測試：使用者查看時，處理中超過 5 分鐘沒報平安 → 「處理中斷，請按重試」
def test_listing_marks_silent_document_interrupted(client, db_session):
    token, ws_id, document_id = _upload(client, "erin@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(6))

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)).json()

    assert documents[0]["status"] == "failed"
    assert documents[0]["error_message"] == "處理中斷，請按重試"


# 測試：處理中、4 分鐘前才報過平安 → 不動（不能把還活著的誤判成中斷）
def test_listing_keeps_document_with_recent_heartbeat(client, db_session):
    token, ws_id, document_id = _upload(client, "frank@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(4))

    documents = client.get(f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)).json()

    assert documents[0]["status"] == "processing"


# 測試：每小時的 sweeper 也會把沒報平安的標中斷
def test_sweep_marks_silent_document_interrupted(client, db_session):
    _, _, document_id = _upload(client, "grace@example.com")
    _set(db_session, document_id, status="processing", claim_token="x", heartbeat_at=_minutes_ago(6))

    assert document_service.expire_overdue_documents(db_session) == 1
    db_session.refresh(db_session.get(Document, document_id))
    assert db_session.get(Document, document_id).error_message == "處理中斷，請按重試"


# 測試：處理到一半，租約被別人接手了（自己被判定中斷）→ 放棄這次處理，這次寫的 chunk 一段都不留，
# 也不碰文件狀態（現在是接手的人的）
def test_worker_gives_up_when_lease_is_taken(client, db_session, monkeypatch):
    _, _, document_id = _upload(client, "henry@example.com")
    document = _set(db_session, document_id, status="uploaded")
    db_session.query(Chunk).filter(Chunk.document_id == document_id).delete()
    db_session.commit()

    # 假的 OpenAI：第一次被呼叫時，模擬「另一個 Lambda 判定我死了、把租約搶走」
    def steal_lease_then_embed(texts, **kwargs):
        with SessionLocal() as other:
            other.query(Document).filter(Document.id == document_id).update(
                {Document.claim_token: "someone-else", Document.heartbeat_at: datetime.now(timezone.utc)}
            )
            other.commit()
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(openai_client, "embed_texts", steal_lease_then_embed)

    document_service.process_document(db_session, document)

    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0
    db_session.refresh(document)
    assert document.status == "processing"
    assert document.claim_token == "someone-else"


# 測試：租約不是自己的了，最後「存結果」也會被擋下來
def test_finish_rejected_with_wrong_token(client, db_session):
    _, _, document_id = _upload(client, "ivy@example.com")
    _set(db_session, document_id, status="processing", claim_token="real", heartbeat_at=datetime.now(timezone.utc))

    assert document_repo.finish_processed(db_session, document_id, "fake") is False
    assert db_session.get(Document, document_id).status == "processing"


# 測試：被判定中斷之後，那個其實只是很慢的 Lambda 再來報平安 → 失敗（租約已經收回），不會把文件「救活」
def test_heartbeat_rejected_after_interrupted(client, db_session):
    token, ws_id, document_id = _upload(client, "jack@example.com")
    _set(db_session, document_id, status="processing", claim_token="slow", heartbeat_at=_minutes_ago(6))
    client.get(f"/workspaces/{ws_id}/documents", headers=_auth_headers(token))  # 觸發判定中斷

    assert document_repo.heartbeat(db_session, document_id, "slow") is False
    db_session.refresh(db_session.get(Document, document_id))
    assert db_session.get(Document, document_id).status == "failed"
