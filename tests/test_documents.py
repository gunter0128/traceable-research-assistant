# document 上傳 + CRUD 的測試：上傳 / 列表 / 查 / 刪 + ownership（透過 workspace）


import pytest

from app.core import openai_client, s3_client
from tests.pdf_fixtures import valid_pdf_bytes
from tests.upload_helpers import upload_pdf

# 每個測試三段（AAA）測試撰寫慣例：
# Arrange ：把系統弄到需要的狀態(不是在測這個 可省略)。
# Act     ：真正要驗證行為的那一個呼叫 存成 response
# Assert  ：檢查 response 對不對


# 上傳現在會呼叫真的 S3 API。這裡用一個記憶體裡的 dict 假裝 S3（upload 存進去、
# download 讀出來、delete 拿掉），跟隔離測試資料庫是同一個道理：測試不該碰到正式的 S3 bucket
@pytest.fixture(autouse=True) # autouse=True 這個檔裡每個測試都自動套用，不用每個測試自己寫進參數列表
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


# 上傳現在會觸發 process_document()，裡面會呼叫真的 OpenAI embedding API。
# 這裡換成固定的假向量：測試不用花錢、不用網路、結果每次都一樣
@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts, **kwargs: [[0.1] * 1536 for _ in texts])


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
    # 要用「真的」能被解析的 PDF，不能隨便塞假 byte —— process_document() 現在會真的
    # 去解析內容，假的 PDF 會讓 status 變成 "failed"，不是這裡要測的東西
    return upload_pdf(client, _auth_headers(token), workspace_id, valid_pdf_bytes(), filename)


# 測試：沒登入不能上傳
def test_upload_requires_auth(client):
    response = client.post(
        "/workspaces/1/uploads", json={"filename": "x.pdf", "size_bytes": 1}
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
    # 上傳完會立刻跑完 process_document()（同步設計），所以這裡已經是 "processed"，
    # 不是 "uploaded"（"uploaded" 只是 DB 那一瞬間的預設值，request 還沒回應完就已經被蓋掉了）
    assert body["status"] == "processed"

    list_response = client.get(
        f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1


# 測試：上傳不是 PDF 的檔案要被擋 400
def test_upload_non_pdf_rejected(client):
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)

    response = upload_pdf(client, _auth_headers(token), ws_id, b"hello", "note.txt")

    assert response.status_code == 400


# ---------- 瀏覽器直接上傳到 S3（docs/decisions/013）----------


def _start(client, token, ws_id, filename="doc.pdf"):
    return client.post(
        f"/workspaces/{ws_id}/uploads",
        json={"filename": filename, "size_bytes": 100},
        headers=_auth_headers(token),
    ).json()


def _finish(client, token, ws_id, key, filename="doc.pdf"):
    return client.post(
        f"/workspaces/{ws_id}/documents",
        json={"key": key, "filename": filename},
        headers=_auth_headers(token),
    )


# 測試：上傳許可的 key 放在這個 workspace 底下，而且不含檔名（檔名可能透露研究內容）
def test_upload_key_is_scoped_to_workspace(client):
    token = _register_and_login(client, "kate@example.com")
    ws_id = _create_workspace(client, token)

    key = _start(client, token, ws_id, "secret-findings.pdf")["key"]

    assert key.startswith(f"documents/{ws_id}/")
    assert "secret" not in key


# 測試：拿到許可卻沒有真的傳到 S3，就說「傳好了」→ 400，不建紀錄
def test_finish_without_file_rejected(client):
    token = _register_and_login(client, "leo@example.com")
    ws_id = _create_workspace(client, token)
    key = _start(client, token, ws_id)["key"]

    response = _finish(client, token, ws_id, key)

    assert response.status_code == 400
    assert client.get(f"/workspaces/{ws_id}/documents", headers=_auth_headers(token)).json() == []


# 測試：拿別的 workspace 的 key 來「認領」檔案 → 400
# （不然 B 只要知道 A 的 key，就能把 A 的檔案建進自己的 workspace 讀內容）
def test_cannot_claim_key_of_other_workspace(client):
    token_a = _register_and_login(client, "mia_a@example.com")
    ws_a = _create_workspace(client, token_a)
    key_a = _start(client, token_a, ws_a)["key"]
    s3_client.upload_file(key_a, valid_pdf_bytes())  # A 真的傳上去了
    token_b = _register_and_login(client, "mia_b@example.com")
    ws_b = _create_workspace(client, token_b)

    response = _finish(client, token_b, ws_b, key_a)

    assert response.status_code == 400


# 測試：同一個檔案說兩次「傳好了」→ 第二次 400，不會有兩筆紀錄指向同一個檔案
# （不然刪掉其中一筆，另一筆的檔案也跟著不見了）
def test_same_key_cannot_be_finished_twice(client, db_session):
    token = _register_and_login(client, "nina@example.com")
    ws_id = _create_workspace(client, token)
    key = _start(client, token, ws_id)["key"]
    s3_client.upload_file(key, valid_pdf_bytes())

    first = _finish(client, token, ws_id, key)
    second = _finish(client, token, ws_id, key)

    assert first.status_code == 201
    assert second.status_code == 400


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
