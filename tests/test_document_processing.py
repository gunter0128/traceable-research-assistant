# process_document() 出錯時的行為：處理到一半失敗、同一份文件處理兩次
# test_documents.py 測的是「正常情況下能動」，這裡測的是「出事時資料還是對的」

import io

import pytest

from app.core import openai_client, s3_client
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import document_service
from tests.pdf_fixtures import valid_pdf_bytes

# 夠長的文字 切下去會變成好幾段 chunk（chunk_size 是 500 token，這裡約 2000 token）
# 只有一段的話測不出「做到一半」的情況
LONG_TEXT = "retrieval " * 2000


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


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


def _upload_long_pdf(client, token, workspace_id):
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        headers=_auth_headers(token),
        files={
            "file": ("long.pdf", io.BytesIO(valid_pdf_bytes(LONG_TEXT)), "application/pdf")
        },
    )


# 假的 embed_text：前幾次正常回傳向量，第 fail_on 次呼叫時丟例外，模擬 OpenAI 中途斷線
def _embedding_that_fails_on(fail_on):
    calls = {"count": 0}

    def fake_embed(text):
        calls["count"] += 1
        if calls["count"] == fail_on:
            raise RuntimeError("模擬 OpenAI timeout")
        return [0.1] * 1536

    return fake_embed


# 測試：第 3 段 embedding 失敗 → 文件標成 failed，而且前 2 段也不能留在資料庫裡
def test_failed_processing_leaves_no_chunks(client, db_session, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_text", _embedding_that_fails_on(3))
    token = _register_and_login(client, "alice@example.com")
    ws_id = _create_workspace(client, token)

    response = _upload_long_pdf(client, token, ws_id)

    assert response.json()["status"] == "failed"
    document_id = response.json()["id"]
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0


# 測試：同一份文件處理兩次（之後背景重試會發生），chunk 不能變成兩倍
def test_reprocessing_does_not_duplicate_chunks(client, db_session, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_text", lambda text: [0.1] * 1536)
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)
    document_id = _upload_long_pdf(client, token, ws_id).json()["id"]
    first_count = db_session.query(Chunk).filter(Chunk.document_id == document_id).count()

    document = db_session.get(Document, document_id)
    document_service.process_document(db_session, document)

    second_count = db_session.query(Chunk).filter(Chunk.document_id == document_id).count()
    assert first_count > 1  # 確認真的切成好幾段，不然這個測試沒有意義
    assert second_count == first_count
