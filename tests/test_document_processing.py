# process_document() 出錯時的行為：處理到一半失敗、同一份文件處理兩次
# test_documents.py 測的是「正常情況下能動」，這裡測的是「出事時資料還是對的」

from app.core import openai_client
from app.models.chunk import Chunk
from app.models.document import Document
from app.services import document_service
from tests.helpers import create_workspace, fake_embed_texts, login_headers, upload_pdf
from tests.pdf_fixtures import blank_pdf_bytes, valid_pdf_bytes

# 夠長的文字 切下去會變成好幾段 chunk（chunk_size 是 500 token，這裡約 2000 token）
# 只有一段的話測不出「做到一半」的情況
LONG_TEXT = "retrieval " * 2000


def _upload_long_pdf(client, headers, workspace_id):
    return upload_pdf(client, headers, workspace_id, valid_pdf_bytes(LONG_TEXT), "long.pdf")


# 假的 embed_texts：前幾次正常回傳向量，第 fail_on 次呼叫時丟例外，模擬中途出錯
# 搭配 EMBED_BATCH_SIZE = 1（一次只送一段）用，「第 3 次呼叫」才會剛好是「第 3 段」
def _embedding_that_fails_on(fail_on):
    calls = {"count": 0}

    def fake_embed(texts, **kwargs):
        calls["count"] += 1
        if calls["count"] == fail_on:
            raise RuntimeError("模擬中途出錯")
        return fake_embed_texts(texts)

    return fake_embed


# 測試：第 3 段 embedding 失敗 → 文件標成 failed，而且前 2 段也不能留在資料庫裡
def test_failed_processing_leaves_no_chunks(client, db_session, monkeypatch):
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    monkeypatch.setattr(openai_client, "embed_texts", _embedding_that_fails_on(3))
    headers = login_headers(client, "alice@example.com")
    ws_id = create_workspace(client, headers)

    response = _upload_long_pdf(client, headers, ws_id)

    assert response.json()["status"] == "failed"
    document_id = response.json()["id"]
    assert db_session.query(Chunk).filter(Chunk.document_id == document_id).count() == 0


# 測試：同一份文件處理兩次（例如重試），chunk 不能變成兩倍
def test_reprocessing_does_not_duplicate_chunks(client, db_session, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)
    headers = login_headers(client, "bob@example.com")
    ws_id = create_workspace(client, headers)
    document_id = _upload_long_pdf(client, headers, ws_id).json()["id"]
    first_count = db_session.query(Chunk).filter(Chunk.document_id == document_id).count()

    document = db_session.get(Document, document_id)
    # 設回排隊中，第二次才搶得到租約、真的再處理一次（處理完的文件不會被重複處理）
    document.status = "uploaded"
    db_session.commit()
    document_service.process_document(db_session, document)

    second_count = db_session.query(Chunk).filter(Chunk.document_id == document_id).count()
    assert first_count > 1  # 確認真的切成好幾段，不然這個測試沒有意義
    assert second_count == first_count


# 測試：第 3 段失敗時，給使用者的是一般性的訊息，不會把內部錯誤細節（"模擬中途出錯"）漏出去
def test_unexpected_error_gives_generic_message(client, monkeypatch):
    monkeypatch.setattr(document_service, "EMBED_BATCH_SIZE", 1)
    monkeypatch.setattr(openai_client, "embed_texts", _embedding_that_fails_on(3))
    headers = login_headers(client, "carol@example.com")
    ws_id = create_workspace(client, headers)

    body = _upload_long_pdf(client, headers, ws_id).json()

    assert body["error_message"] == "處理時發生錯誤，可以稍後重試"


# 測試：掃描檔（抓不到任何字）不能被標成「完成」，要標失敗並說明原因
def test_pdf_without_text_is_failed_not_processed(client, db_session):
    headers = login_headers(client, "dave@example.com")
    ws_id = create_workspace(client, headers)

    body = upload_pdf(client, headers, ws_id, blank_pdf_bytes()).json()

    assert body["status"] == "failed"
    assert "掃描檔" in body["error_message"]
    assert db_session.query(Chunk).filter(Chunk.document_id == body["id"]).count() == 0


# 測試：壞掉的 PDF → 失敗原因是「無法讀取」
def test_corrupt_pdf_has_reason(client):
    headers = login_headers(client, "erin@example.com")
    ws_id = create_workspace(client, headers)

    body = upload_pdf(client, headers, ws_id, b"not really a pdf").json()

    assert body["status"] == "failed"
    assert body["error_message"] == "PDF 檔案損壞或無法讀取"


# 測試：超過頁數上限 → 失敗原因寫明上限（把上限暫時改成 0，1 頁的 PDF 就超過了）
def test_too_many_pages_has_reason(client, monkeypatch):
    monkeypatch.setattr(document_service, "MAX_PAGES", 0)
    headers = login_headers(client, "frank@example.com")
    ws_id = create_workspace(client, headers)

    body = upload_pdf(client, headers, ws_id, valid_pdf_bytes()).json()

    assert body["status"] == "failed"
    assert body["error_message"] == "超過 0 頁的上限"


# 測試：檔案超過大小上限 → 413，而且根本不會建立文件（把上限暫時改成 10 bytes）
def test_file_too_large_rejected(client, monkeypatch):
    monkeypatch.setattr(document_service, "MAX_UPLOAD_BYTES", 10)
    headers = login_headers(client, "grace@example.com")
    ws_id = create_workspace(client, headers)

    response = upload_pdf(client, headers, ws_id, valid_pdf_bytes())

    assert response.status_code == 413
    documents = client.get(f"/workspaces/{ws_id}/documents", headers=headers).json()
    assert documents == []
