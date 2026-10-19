# 重複文件的測試（docs/decisions/022）：同一個 workspace 上傳內容一模一樣的 PDF，第二份標成失敗


import pytest

from app.core import openai_client, s3_client
from tests.pdf_fixtures import valid_pdf_bytes
from tests.upload_helpers import upload_pdf


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


# 假的轉向量，順便記下被呼叫幾次：重複的文件不應該花轉向量的錢
@pytest.fixture
def embed_calls(monkeypatch):
    calls = []

    def fake_embed(texts, **kwargs):
        calls.append(len(texts))
        return [[0.1] * 1536 for _ in texts]

    monkeypatch.setattr(openai_client, "embed_texts", fake_embed)
    return calls


def _headers(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    token = client.post("/auth/login", json={"email": email, "password": "pw123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _workspace(client, headers, name="研究"):
    return client.post("/workspaces", json={"name": name}, headers=headers).json()["id"]


# 測試：同一個 workspace 傳兩次同一份檔案（檔名不同也一樣），第二份失敗，訊息說跟哪一份重複，而且沒有轉向量
def test_same_content_in_same_workspace_is_rejected(client, embed_calls):
    headers = _headers(client, "dup1@example.com")
    ws_id = _workspace(client, headers)
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
    headers = _headers(client, "dup2@example.com")
    content = valid_pdf_bytes("Multi-hop retrieval")

    first = upload_pdf(client, headers, _workspace(client, headers, "A"), content, "IRCoT.pdf")
    second = upload_pdf(client, headers, _workspace(client, headers, "B"), content, "IRCoT.pdf")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "processed"


# 測試：內容不同就不算重複，就算檔名一樣
def test_different_content_is_not_duplicate(client, embed_calls):
    headers = _headers(client, "dup3@example.com")
    ws_id = _workspace(client, headers)

    first = upload_pdf(client, headers, ws_id, valid_pdf_bytes("version one"), "report.pdf")
    second = upload_pdf(client, headers, ws_id, valid_pdf_bytes("version two"), "report.pdf")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "processed"


# 測試：把原本那份刪掉之後，同樣的內容可以重新上傳
def test_can_upload_again_after_deleting_original(client, embed_calls):
    headers = _headers(client, "dup4@example.com")
    ws_id = _workspace(client, headers)
    content = valid_pdf_bytes("Multi-hop retrieval")
    first = upload_pdf(client, headers, ws_id, content, "IRCoT.pdf")

    client.delete(f"/documents/{first.json()['id']}", headers=headers)
    again = upload_pdf(client, headers, ws_id, content, "IRCoT.pdf")

    assert again.json()["status"] == "processed"
