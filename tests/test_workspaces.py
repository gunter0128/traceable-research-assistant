# workspace CRUD 的測試：建立 / 列表 / 查 / 改 / 刪 + ownership
# 建 workspace 的請求刻意直接寫出來、不用 helpers.create_workspace：這個檔案測的就是這些 endpoint

from tests.helpers import login_headers


# 測試：沒登入不能建 workspace
def test_create_workspace_requires_auth(client):
    response = client.post("/workspaces", json={"name": "no auth"})
    assert response.status_code == 401


# 測試：建立成功後，列表看得到自己剛建的
def test_create_and_list_own_workspace(client):
    headers = login_headers(client, "alice@example.com")

    create_response = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=headers
    )
    assert create_response.status_code == 201

    list_response = client.get("/workspaces", headers=headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["name"] == "A的研究"


# 測試：B 看不到 A 的 workspace，列表是空的、查單一筆是 403
def test_cannot_see_others_workspace(client):
    headers_a = login_headers(client, "bob_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=headers_a
    ).json()["id"]

    headers_b = login_headers(client, "bob_b@example.com")

    list_response = client.get("/workspaces", headers=headers_b)
    assert list_response.json() == []

    get_response = client.get(f"/workspaces/{ws_id}", headers=headers_b)
    assert get_response.status_code == 403


# 測試：查一個不存在的 id 要回 404
def test_get_nonexistent_workspace(client):
    headers = login_headers(client, "carol@example.com")

    response = client.get("/workspaces/999999", headers=headers)

    assert response.status_code == 404


# 測試：自己的 workspace 可以改名
def test_update_own_workspace(client):
    headers = login_headers(client, "dave@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "舊名字"}, headers=headers
    ).json()["id"]

    response = client.put(
        f"/workspaces/{ws_id}", json={"name": "新名字"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["name"] == "新名字"


# 測試：B 不能改 A 的 workspace
def test_cannot_update_others_workspace(client):
    headers_a = login_headers(client, "erin_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=headers_a
    ).json()["id"]

    headers_b = login_headers(client, "erin_b@example.com")
    response = client.put(
        f"/workspaces/{ws_id}", json={"name": "hacked"}, headers=headers_b
    )

    assert response.status_code == 403


# 測試：刪除自己的 workspace 後，再查會變 404
def test_delete_own_workspace(client):
    headers = login_headers(client, "frank@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "要刪的"}, headers=headers
    ).json()["id"]

    delete_response = client.delete(
        f"/workspaces/{ws_id}", headers=headers
    )
    assert delete_response.status_code == 204

    get_response = client.get(f"/workspaces/{ws_id}", headers=headers)
    assert get_response.status_code == 404


# 測試：B 不能刪 A 的 workspace
def test_cannot_delete_others_workspace(client):
    headers_a = login_headers(client, "gina_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=headers_a
    ).json()["id"]

    headers_b = login_headers(client, "gina_b@example.com")
    response = client.delete(f"/workspaces/{ws_id}", headers=headers_b)

    assert response.status_code == 403
