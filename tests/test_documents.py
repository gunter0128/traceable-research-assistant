# document 上傳 + CRUD 的測試：上傳 / 列表 / 查 / 刪 + ownership（透過 workspace）

import io

import pytest

from app.services import document_service

# 每個測試三段（AAA）測試撰寫慣例：
# Arrange ：把系統弄到需要的狀態(不是在測這個 可省略)。
# Act     ：真正要驗證行為的那一個呼叫 存成 response
# Assert  ：檢查 response 對不對


# 上傳會真的把檔案寫到硬碟。這個 fixture 把存檔位置換成 pytest 給的臨時資料夾，
# 跟隔離測試資料庫是同一個道理：測試不該碰到 storage/documents/ 這個正式資料夾。
@pytest.fixture(autouse=True) # autouse=True 這個檔裡每個測試都自動套用，不用每個測試自己寫進參數列表
# tmp_path / monkeypatch 都是 pytest 內建的 fixture 不用自己寫或 import
# tmp_path：每次用都給一個全新、專屬這次測試的空資料夾路徑，測完 pytest 自己清
# monkeypatch：臨時改掉某個東西，測試結束自動改回原值，不用自己寫收尾
def _use_temp_storage(tmp_path, monkeypatch):
    # setattr(物件, "屬性名字", 新值) 把 document_service 的 STORAGE_DIR 換成這次的臨時資料夾
    # 跟內建 setattr() 做的事一樣 差別是 monkeypatch.setattr 會記住舊值 測試結束自動換回去
    monkeypatch.setattr(document_service, "STORAGE_DIR", tmp_path)


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


def _upload_pdf(client, token, workspace_id, filename="IRCoT.pdf"):
    return client.post(
        f"/workspaces/{workspace_id}/documents",
        headers=_auth_headers(token),
        files={"file": (filename, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )


# 測試：沒登入不能上傳
def test_upload_requires_auth(client):
    response = client.post(
        "/workspaces/1/documents",
        files={"file": ("x.pdf", io.BytesIO(b"x"), "application/pdf")},
    )
    assert response.status_code == 401


# 測試：上傳成功，回傳正確欄位，而且列表看得到
def test_upload_and_list(client):
    token = _register_and_login(client, "alice@example.com")
    ws_id = _create_workspace(client, token)

    upload_response = _upload_pdf(client, token, ws_id)
    assert upload_response.status_code == 201
    body = upload_response.json()
    assert body["filename"] == "IRCoT.pdf"
    assert body["status"] == "uploaded"

    list_response = client.get(
        f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1


# 測試：上傳不是 PDF 的檔案要被擋 400
def test_upload_non_pdf_rejected(client):
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)

    response = client.post(
        f"/workspaces/{ws_id}/documents",
        headers=_auth_headers(token),
        files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")},
    )

    assert response.status_code == 400


# 測試：B 不能傳文件到 A 的 workspace
def test_cannot_upload_to_others_workspace(client):
    token_a = _register_and_login(client, "carol_a@example.com")
    ws_id = _create_workspace(client, token_a)

    token_b = _register_and_login(client, "carol_b@example.com")
    response = _upload_pdf(client, token_b, ws_id)

    assert response.status_code == 403


# 測試：查自己的單一份文件
def test_get_document(client):
    token = _register_and_login(client, "dave@example.com")
    ws_id = _create_workspace(client, token)
    doc_id = _upload_pdf(client, token, ws_id).json()["id"]

    response = client.get(f"/documents/{doc_id}", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["id"] == doc_id


# 測試：B 查 A 的文件要被擋 403（透過 workspace 判斷 ownership）
def test_cannot_get_others_document(client):
    token_a = _register_and_login(client, "erin_a@example.com")
    ws_id = _create_workspace(client, token_a)
    doc_id = _upload_pdf(client, token_a, ws_id).json()["id"]

    token_b = _register_and_login(client, "erin_b@example.com")
    response = client.get(f"/documents/{doc_id}", headers=_auth_headers(token_b))

    assert response.status_code == 403


# 測試：查不存在的文件 id 要回 404
def test_get_nonexistent_document(client):
    token = _register_and_login(client, "frank@example.com")

    response = client.get("/documents/999999", headers=_auth_headers(token))

    assert response.status_code == 404


# 測試：刪除自己的文件後，再查會變 404
def test_delete_own_document(client):
    token = _register_and_login(client, "gina@example.com")
    ws_id = _create_workspace(client, token)
    doc_id = _upload_pdf(client, token, ws_id).json()["id"]

    delete_response = client.delete(
        f"/documents/{doc_id}", headers=_auth_headers(token)
    )
    assert delete_response.status_code == 204

    get_response = client.get(f"/documents/{doc_id}", headers=_auth_headers(token))
    assert get_response.status_code == 404


# 測試：B 不能刪 A 的文件
def test_cannot_delete_others_document(client):
    token_a = _register_and_login(client, "henry_a@example.com")
    ws_id = _create_workspace(client, token_a)
    doc_id = _upload_pdf(client, token_a, ws_id).json()["id"]

    token_b = _register_and_login(client, "henry_b@example.com")
    response = client.delete(f"/documents/{doc_id}", headers=_auth_headers(token_b))

    assert response.status_code == 403
