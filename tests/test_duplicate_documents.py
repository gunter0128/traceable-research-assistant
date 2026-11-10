# 重複文件的測試（docs/decisions/022）：同一個 workspace 上傳內容一模一樣的 PDF，第二份標成失敗


import pytest

from app.core import openai_client
from tests.helpers import create_workspace, fake_embed_texts, login_headers, upload_pdf
from tests.pdf_fixtures import valid_pdf_bytes


# 假的轉向量，順便記下被呼叫幾次：重複的文件不應該花轉向量的錢
@pytest.fixture
def embed_calls(monkeypatch):
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append(len(texts))
        return fake_embed_texts(texts)

    monkeypatch.setattr(openai_client, "embed_texts", fake_embed)
    return calls


# 測試：同一個 workspace 傳兩次同一份檔案（檔名不同也一樣），第二份失敗，訊息說跟哪一份重複，而且沒有轉向量
def test_same_content_in_same_workspace_is_rejected(client, embed_calls):
    headers = login_headers(client, "dup1@example.com")
    ws_id = create_workspace(client, headers)
    content = valid_pdf_bytes("Multi-hop retrieval")

    first = upload_pdf(client, headers, ws_id, content, "IRCoT.pdf")
    calls_after_first = len(embed_calls)
    second = upload_pdf(client, headers, ws_id, content, "IRCoT (1).pdf")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "failed"
    assert "IRCoT.pdf" in second.json()["error_message"]
    assert len(embed_calls) == calls_after_first


# 測試：不同 workspace 各自有一份是正常的（不同專案可能都要用同一篇論文）
def test_same_content_in_other_workspace_is_allowed(client, embed_calls):
    headers = login_headers(client, "dup2@example.com")
    content = valid_pdf_bytes("Multi-hop retrieval")

    first = upload_pdf(client, headers, create_workspace(client, headers, "A"), content, "IRCoT.pdf")
    second = upload_pdf(client, headers, create_workspace(client, headers, "B"), content, "IRCoT.pdf")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "processed"


# 測試：內容不同就不算重複，就算檔名一樣
def test_different_content_is_not_duplicate(client, embed_calls):
    headers = login_headers(client, "dup3@example.com")
    ws_id = create_workspace(client, headers)

    first = upload_pdf(client, headers, ws_id, valid_pdf_bytes("version one"), "report.pdf")
    second = upload_pdf(client, headers, ws_id, valid_pdf_bytes("version two"), "report.pdf")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "processed"


# 測試：把原本那份刪掉之後，同樣的內容可以重新上傳
def test_can_upload_again_after_deleting_original(client, embed_calls):
    headers = login_headers(client, "dup4@example.com")
    ws_id = create_workspace(client, headers)
    content = valid_pdf_bytes("Multi-hop retrieval")
    first = upload_pdf(client, headers, ws_id, content, "IRCoT.pdf")

    client.delete(f"/documents/{first.json()['id']}", headers=headers)
    again = upload_pdf(client, headers, ws_id, content, "IRCoT.pdf")

    assert again.json()["status"] == "processed"
