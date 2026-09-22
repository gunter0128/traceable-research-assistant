# /ask 端點的測試：問答 + ownership。真的打 OpenAI 的部分全部 mock 掉，不花錢、結果每次都一樣

import io

import pytest

from app.core import openai_client
from app.services import document_service
from tests.pdf_fixtures import valid_pdf_bytes

# autouse=True 的意思是：「不用等有人在參數列表裡點名要你，這個檔案裡每一個測試開始前，自動先跑我」
# 所以這個 fixture 不會看到有人呼叫他
@pytest.fixture(autouse=True)
def _use_temp_storage(tmp_path, monkeypatch):
    # tmp_path / monkeypatch 都是 pytest 內建的 fixture 不用自己寫或 import
    # tmp_path：每次用都給一個全新、專屬這次測試的空資料夾路徑，測完 pytest 自己清
    # monkeypatch：臨時改掉某個東西，測試結束自動改回原值，不用自己寫收尾
    monkeypatch.setattr(document_service, "STORAGE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    # 上傳跟問問題都會呼叫 embed_text，兩邊用的是同一個函式，這裡一次擋掉
    monkeypatch.setattr(openai_client, "embed_text", lambda text: [0.1] * 1536)


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


def _upload_pdf(client, token, workspace_id, text="Multi-hop retrieval is the core method"):
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        headers=_auth_headers(token),
        files={
            "file": ("thesis.pdf", io.BytesIO(valid_pdf_bytes(text)), "application/pdf") # io.BytesIO(bytes資料) 把一包 bytes包裝成一個檔案物件
        },
    )


# 測試：沒登入不能問
def test_ask_requires_auth(client):
    response = client.post("/workspaces/1/ask", json={"question": "?"})
    assert response.status_code == 401


# 測試：workspace 不存在回 404
def test_ask_nonexistent_workspace(client):
    token = _register_and_login(client, "alice@example.com")
    response = client.post(
        "/workspaces/999999/ask",
        json={"question": "?"},
        headers=_auth_headers(token),
    )
    assert response.status_code == 404


# 測試：B 不能問 A 的 workspace
def test_cannot_ask_others_workspace(client):
    token_a = _register_and_login(client, "bob_a@example.com")
    ws_id = _create_workspace(client, token_a)

    token_b = _register_and_login(client, "bob_b@example.com")
    response = client.post(
        f"/workspaces/{ws_id}/ask",
        json={"question": "?"},
        headers=_auth_headers(token_b),
    )
    assert response.status_code == 403


# 測試：正常問答，answer 跟 citations 的形狀跟內容都對
# generate_answer 也 mock 掉——這裡不測 LLM 答得好不好，只測我們自己寫的串接邏輯
def test_ask_returns_answer_with_citations(client, monkeypatch):
    monkeypatch.setattr(
        openai_client, "generate_answer", lambda question, context: "這是生成的答案"
    )

    # valid_pdf_bytes() 手刻的內容流只支援 latin-1，塞中文會編碼失敗，這裡故意用英文內容
    token = _register_and_login(client, "carol@example.com")
    ws_id = _create_workspace(client, token)
    upload = _upload_pdf(client, token, ws_id, text="Multi-hop retrieval is the core method")
    assert upload.json()["status"] == "processed"

    response = client.post(
        f"/workspaces/{ws_id}/ask",
        json={"question": "這篇論文的方法是什麼？"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "這是生成的答案"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["filename"] == "thesis.pdf"
    assert "Multi-hop retrieval" in body["citations"][0]["content"]


# 測試：workspace 裡還沒有任何文件時問問題，不會噴錯，只是 citations 是空的
def test_ask_with_no_documents_returns_empty_citations(client, monkeypatch):
    monkeypatch.setattr(
        openai_client, "generate_answer", lambda question, context: "文件中找不到相關資訊"
    )

    token = _register_and_login(client, "dave@example.com")
    ws_id = _create_workspace(client, token)

    response = client.post(
        f"/workspaces/{ws_id}/ask",
        json={"question": "這篇論文的方法是什麼？"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["citations"] == []
