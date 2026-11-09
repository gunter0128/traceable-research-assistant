# S3 跟資料庫的一致性（docs/decisions/006）：兩邊沒辦法包在同一個 transaction 裡
# 測的是：中途失敗時只會留下孤兒檔案（無害），不會留下壞掉的紀錄（有害）；孤兒檔案會被對帳清掉

from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app import worker
from app.core import openai_client, s3_client
from app.core.config import settings
from app.models.document import Document
from app.repositories import document_repo
from app.services import document_service
from tests.helpers import (
    always_raise,
    create_workspace,
    fake_embed_texts,
    login_headers,
    upload_pdf,
)
from tests.pdf_fixtures import valid_pdf_bytes


# 對帳要靠「最後修改時間」判斷檔案放多久了：conftest.py 的 fake_s3 把每個檔案都當成剛放進去，
# 這裡改成照 modified 記的時間（key → 最後修改時間），沒記的才當成剛放進去
@pytest.fixture
def modified(monkeypatch, fake_s3):
    times = {}
    monkeypatch.setattr(
        s3_client,
        "list_files",
        lambda prefix: [(key, times.get(key, datetime.now(timezone.utc))) for key in fake_s3 if key.startswith(prefix)],
    )
    return times


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)


def _upload(client, headers, ws_id):
    return upload_pdf(client, headers, ws_id, valid_pdf_bytes())


# 測試：檔案存進 S3 了，建紀錄卻失敗 → 把剛存的檔案刪掉，不留孤兒檔案
def test_upload_removes_file_when_record_fails(client, monkeypatch, fake_s3):
    headers = login_headers(client, "alice@example.com")
    ws_id = create_workspace(client, headers)

    def broken_create(*args, **kwargs):
        raise RuntimeError("模擬資料庫寫入失敗")

    monkeypatch.setattr(document_repo, "create", broken_create)

    # TestClient 遇到 API 沒接住的錯誤，會直接在測試裡丟出來（真的部署時是回 500）
    with pytest.raises(RuntimeError):
        _upload(client, headers, ws_id)

    assert fake_s3 == {}


# 測試：刪文件時 S3 出錯 → 紀錄還是要刪掉（先刪紀錄），只留下孤兒檔案，不會留下「看得到卻壞掉」的文件
def test_delete_document_succeeds_even_if_s3_fails(client, monkeypatch, fake_s3):
    headers = login_headers(client, "bob@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload(client, headers, ws_id).json()["id"]
    monkeypatch.setattr(s3_client, "delete_file", always_raise(s3_client.StorageTemporaryError("模擬 S3 出錯")))

    response = client.delete(f"/documents/{document_id}", headers=headers)

    assert response.status_code == 204
    assert client.get(f"/documents/{document_id}", headers=headers).status_code == 404
    assert len(fake_s3) == 1  # 孤兒檔案，等對帳清


# 測試：刪 workspace → 底下文件的 S3 檔案也一起刪（以前只刪資料庫紀錄，檔案會一直留著）
def test_delete_workspace_removes_files(client, fake_s3):
    headers = login_headers(client, "carol@example.com")
    ws_id = create_workspace(client, headers)
    _upload(client, headers, ws_id)
    _upload(client, headers, ws_id)
    assert len(fake_s3) == 2

    client.delete(f"/workspaces/{ws_id}", headers=headers)

    assert fake_s3 == {}


# 測試：對帳 → 只刪「沒有紀錄、而且放超過 1 天」的檔案；有紀錄的、剛放進去的都不動
def test_orphan_cleanup_only_deletes_old_unreferenced_files(client, db_session, fake_s3, modified):
    headers = login_headers(client, "dave@example.com")
    ws_id = create_workspace(client, headers)
    _upload(client, headers, ws_id)
    referenced_key = next(iter(fake_s3))
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    modified[referenced_key] = two_days_ago  # 有紀錄的檔案，就算很舊也不能刪
    fake_s3["documents/old_orphan.pdf"] = b""
    modified["documents/old_orphan.pdf"] = two_days_ago  # 孤兒、而且很舊 → 刪
    fake_s3["documents/new_orphan.pdf"] = b""  # 孤兒、但剛放進去（可能正在上傳）→ 不刪

    deleted = document_service.delete_orphan_files(db_session)

    assert deleted == 1
    assert set(fake_s3) == {referenced_key, "documents/new_orphan.pdf"}


# 測試：每小時的 sweeper 會順便對帳，回傳值裡有刪了幾個
def test_sweep_also_cleans_orphans(monkeypatch, db_session, fake_s3, modified):
    monkeypatch.setattr(worker, "SessionLocal", lambda: db_session)
    fake_s3["documents/old_orphan.pdf"] = b""
    modified["documents/old_orphan.pdf"] = datetime.now(timezone.utc) - timedelta(days=2)

    result = worker.handle_sweep(None, None)

    assert result == {"expired": 0, "orphans_deleted": 1}


# ---------- S3 錯誤的分類（docs/failure-modes.md 的缺口 1、2）----------
# 這裡要測真的 download_file：conftest.py 的 fake_s3 每個測試開始前會把它換成假的，
# 所以在載入這個檔案時（還沒被換掉）先記下真的那個；它裡面呼叫的 client.get_object 再換成丟錯誤的假函式
REAL_DOWNLOAD_FILE = s3_client.download_file


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
        REAL_DOWNLOAD_FILE("documents/x.pdf")


# 測試：處理時檔案不見了 → 標失敗，訊息叫使用者重新上傳（不能叫他「稍後重試」，重試永遠不會成功）
def test_missing_file_fails_with_clear_reason(client, db_session, monkeypatch):
    headers = login_headers(client, "erin@example.com")
    ws_id = create_workspace(client, headers)
    monkeypatch.setattr(s3_client, "download_file", always_raise(s3_client.FileMissing("NoSuchKey")))
    # 上傳完成時檔案還在（確認得到大小），處理時才不見
    monkeypatch.setattr(s3_client, "file_size", lambda key: 100)

    body = _upload(client, headers, ws_id).json()

    assert body["status"] == "failed"
    assert body["error_message"] == "找不到上傳的檔案，請刪除後重新上傳"


# 測試：雲端、處理時 S3 暫時連不上 → 跟 OpenAI 一樣直接標失敗，請使用者稍後按重試（不延後，docs/decisions/046）
def test_storage_temporary_error_fails_in_cloud(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "sqs_queue_url", "https://fake-queue")
    monkeypatch.setattr(document_service.sqs_client, "send_process_message", lambda document_id: None)
    headers = login_headers(client, "frank@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload(client, headers, ws_id).json()["id"]
    document = db_session.get(Document, document_id)
    monkeypatch.setattr(s3_client, "download_file", always_raise(s3_client.StorageTemporaryError("模擬")))

    document_service.process_document(db_session, document)

    db_session.refresh(document)
    assert document.status == "failed"
    assert document.error_message == "儲存服務暫時無法使用，請稍後按重試"


# 測試：上傳完成、API 去 S3 確認檔案時 S3 暫時連不上 → 回 503 和看得懂的訊息（不是 500），不建紀錄
def test_upload_returns_503_when_storage_unavailable(client, monkeypatch):
    headers = login_headers(client, "grace@example.com")
    ws_id = create_workspace(client, headers)
    monkeypatch.setattr(s3_client, "file_size", always_raise(s3_client.StorageTemporaryError("模擬")))

    response = _upload(client, headers, ws_id)

    assert response.status_code == 503
    assert response.json()["detail"] == "儲存服務暫時無法使用，請稍後再試"
    assert client.get(f"/workspaces/{ws_id}/documents", headers=headers).json() == []
