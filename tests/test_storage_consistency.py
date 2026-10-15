# S3 跟資料庫的一致性（docs/decisions/006）：兩邊沒辦法包在同一個 transaction 裡
# 測的是：中途失敗時只會留下孤兒檔案（無害），不會留下壞掉的紀錄（有害）；孤兒檔案會被對帳清掉

import io
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app import worker
from app.core import openai_client, s3_client
from app.core.config import settings
from app.models.document import Document
from app.repositories import document_repo
from app.services import document_service
from tests.pdf_fixtures import valid_pdf_bytes


# 假的 S3：比其他測試檔多記一個「最後修改時間」，對帳要靠它判斷檔案放多久了
@pytest.fixture
def bucket(monkeypatch):
    files = {}
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: files.__setitem__(key, now))
    monkeypatch.setattr(s3_client, "download_file", lambda key: valid_pdf_bytes())
    monkeypatch.setattr(s3_client, "delete_file", lambda key: files.pop(key, None))
    monkeypatch.setattr(
        s3_client,
        "list_files",
        lambda prefix: [(key, modified) for key, modified in files.items() if key.startswith(prefix)],
    )
    return files


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts: [[0.1] * 1536 for _ in texts])


def _register_and_login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    response = client.post("/auth/login", json={"email": email, "password": "pw123456"})
    return response.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _create_workspace(client, token):
    return client.post("/workspaces", json={"name": "研究"}, headers=_auth_headers(token)).json()["id"]


def _upload(client, token, ws_id):
    return client.post(
        f"/workspaces/{ws_id}/documents",
        headers=_auth_headers(token),
        files={"file": ("doc.pdf", io.BytesIO(valid_pdf_bytes()), "application/pdf")},
    )


def _s3_error(*args, **kwargs):
    raise s3_client.StorageTemporaryError("模擬 S3 出錯")


# 測試：檔案存進 S3 了，建紀錄卻失敗 → 把剛存的檔案刪掉，不留孤兒檔案
def test_upload_removes_file_when_record_fails(client, monkeypatch, bucket):
    token = _register_and_login(client, "alice@example.com")
    ws_id = _create_workspace(client, token)

    def broken_create(*args, **kwargs):
        raise RuntimeError("模擬資料庫寫入失敗")

    monkeypatch.setattr(document_repo, "create", broken_create)

    # TestClient 遇到 API 沒接住的錯誤，會直接在測試裡丟出來（真的部署時是回 500）
    with pytest.raises(RuntimeError):
        _upload(client, token, ws_id)

    assert bucket == {}


# 測試：刪文件時 S3 出錯 → 紀錄還是要刪掉（先刪紀錄），只留下孤兒檔案，不會留下「看得到卻壞掉」的文件
def test_delete_document_succeeds_even_if_s3_fails(client, monkeypatch, bucket):
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload(client, token, ws_id).json()["id"]
    monkeypatch.setattr(s3_client, "delete_file", _s3_error)

    response = client.delete(f"/documents/{document_id}", headers=_auth_headers(token))

    assert response.status_code == 204
    assert client.get(f"/documents/{document_id}", headers=_auth_headers(token)).status_code == 404
    assert len(bucket) == 1  # 孤兒檔案，等對帳清


# 測試：刪 workspace → 底下文件的 S3 檔案也一起刪（以前只刪資料庫紀錄，檔案會一直留著）
def test_delete_workspace_removes_files(client, bucket):
    token = _register_and_login(client, "carol@example.com")
    ws_id = _create_workspace(client, token)
    _upload(client, token, ws_id)
    _upload(client, token, ws_id)
    assert len(bucket) == 2

    client.delete(f"/workspaces/{ws_id}", headers=_auth_headers(token))

    assert bucket == {}


# 測試：對帳 → 只刪「沒有紀錄、而且放超過 1 天」的檔案；有紀錄的、剛放進去的都不動
def test_orphan_cleanup_only_deletes_old_unreferenced_files(client, db_session, bucket):
    token = _register_and_login(client, "dave@example.com")
    ws_id = _create_workspace(client, token)
    _upload(client, token, ws_id)
    referenced_key = next(iter(bucket))
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    bucket[referenced_key] = two_days_ago  # 有紀錄的檔案，就算很舊也不能刪
    bucket["documents/old_orphan.pdf"] = two_days_ago  # 孤兒、而且很舊 → 刪
    bucket["documents/new_orphan.pdf"] = datetime.now(timezone.utc)  # 孤兒、但剛放進去（可能正在上傳）→ 不刪

    deleted = document_service.delete_orphan_files(db_session)

    assert deleted == 1
    assert set(bucket) == {referenced_key, "documents/new_orphan.pdf"}


# 測試：每小時的 sweeper 會順便對帳，回傳值裡有刪了幾個
def test_sweep_also_cleans_orphans(monkeypatch, db_session, bucket):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    bucket["documents/old_orphan.pdf"] = datetime.now(timezone.utc) - timedelta(days=2)

    result = worker.handle_sweep(None, None)

    assert result == {"expired": 0, "orphans_deleted": 1}


# ---------- S3 錯誤的分類（docs/failure-modes.md 的缺口 1、2）----------


def _client_error(code, status):
    return ClientError(
        {"Error": {"Code": code, "Message": "模擬"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_client_error("NoSuchKey", 404), s3_client.FileMissing),
        (_client_error("AccessDenied", 403), s3_client.StoragePermanentError),
        (_client_error("SlowDown", 503), s3_client.StorageTemporaryError),
        (_client_error("InternalError", 500), s3_client.StorageTemporaryError),
        (EndpointConnectionError(endpoint_url="https://s3"), s3_client.StorageTemporaryError),
    ],
)
# 測試：boto3 丟出來的錯誤，會被翻譯成正確的種類（檔案不存在 / 永久性 / 暫時性）
def test_s3_errors_are_classified(monkeypatch, exc, expected):
    def fake_get_object(**kwargs):
        raise exc

    monkeypatch.setattr(s3_client.client, "get_object", fake_get_object)

    with pytest.raises(expected):
        s3_client.download_file("documents/x.pdf")


# 測試：處理時檔案不見了 → 標失敗，訊息叫使用者重新上傳（不能叫他「稍後重試」，重試永遠不會成功）
def test_missing_file_fails_with_clear_reason(client, db_session, monkeypatch, bucket):
    token = _register_and_login(client, "erin@example.com")
    ws_id = _create_workspace(client, token)
    monkeypatch.setattr(s3_client, "download_file", _raise(s3_client.FileMissing("NoSuchKey")))

    body = _upload(client, token, ws_id).json()

    assert body["status"] == "failed"
    assert body["error_message"] == "找不到上傳的檔案，請刪除後重新上傳"


# 測試：雲端、處理時 S3 暫時連不上 → 跟 OpenAI 一樣，留在處理中等下一回合
def test_storage_temporary_error_defers_in_cloud(client, db_session, monkeypatch, bucket):
    monkeypatch.setattr(settings, "sqs_queue_url", "https://fake-queue")
    monkeypatch.setattr(document_service.sqs_client, "send_process_message", lambda document_id: None)
    token = _register_and_login(client, "frank@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload(client, token, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    monkeypatch.setattr(s3_client, "download_file", _raise(s3_client.StorageTemporaryError("模擬")))

    with pytest.raises(document_service.RetryLater):
        document_service.process_document(db_session, document)

    db_session.refresh(document)
    assert document.status == "uploaded"
    assert document.error_message == "儲存服務暫時無法使用，稍後會自動重試"


# 測試：上傳時 S3 暫時連不上 → 回 503 和看得懂的訊息（以前是 500），什麼都不存
def test_upload_returns_503_when_storage_unavailable(client, monkeypatch, bucket):
    token = _register_and_login(client, "grace@example.com")
    ws_id = _create_workspace(client, token)
    monkeypatch.setattr(s3_client, "upload_file", _raise(s3_client.StorageTemporaryError("模擬")))

    response = _upload(client, token, ws_id)

    assert response.status_code == 503
    assert response.json()["detail"] == "儲存服務暫時無法使用，請稍後再試"
    assert client.get(f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)).json() == []


def _raise(exc):
    def fake(*args, **kwargs):
        raise exc

    return fake
