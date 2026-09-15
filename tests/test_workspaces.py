# workspace CRUD 的測試：建立 / 列表 / 查 / 改 / 刪 + ownership


# 共用的小工具：註冊 + 登入一個使用者，回傳他的 token（不是 fixture，單純一個函式）
def _register_and_login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    response = client.post(
        "/auth/login", json={"email": email, "password": "pw123456"}
    )
    return response.json()["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# 測試：沒登入不能建 workspace
def test_create_workspace_requires_auth(client):
    response = client.post("/workspaces", json={"name": "no auth"})
    assert response.status_code == 401


# 測試：建立成功後，列表看得到自己剛建的
def test_create_and_list_own_workspace(client):
    token = _register_and_login(client, "alice@example.com")

    create_response = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=_auth_headers(token)
    )
    assert create_response.status_code == 201

    list_response = client.get("/workspaces", headers=_auth_headers(token))
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["name"] == "A的研究"


# 測試：B 看不到 A 的 workspace，列表是空的、查單一筆是 403
def test_cannot_see_others_workspace(client):
    token_a = _register_and_login(client, "bob_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=_auth_headers(token_a)
    ).json()["id"]

    token_b = _register_and_login(client, "bob_b@example.com")

    list_response = client.get("/workspaces", headers=_auth_headers(token_b))
    assert list_response.json() == []

    get_response = client.get(f"/workspaces/{ws_id}", headers=_auth_headers(token_b))
    assert get_response.status_code == 403


# 測試：查一個不存在的 id 要回 404
def test_get_nonexistent_workspace(client):
    token = _register_and_login(client, "carol@example.com")

    response = client.get("/workspaces/999999", headers=_auth_headers(token))

    assert response.status_code == 404


# 測試：自己的 workspace 可以改名
def test_update_own_workspace(client):
    token = _register_and_login(client, "dave@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "舊名字"}, headers=_auth_headers(token)
    ).json()["id"]

    response = client.put(
        f"/workspaces/{ws_id}", json={"name": "新名字"}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["name"] == "新名字"


# 測試：B 不能改 A 的 workspace
def test_cannot_update_others_workspace(client):
    token_a = _register_and_login(client, "erin_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=_auth_headers(token_a)
    ).json()["id"]

    token_b = _register_and_login(client, "erin_b@example.com")
    response = client.put(
        f"/workspaces/{ws_id}", json={"name": "hacked"}, headers=_auth_headers(token_b)
    )

    assert response.status_code == 403


# 測試：刪除自己的 workspace 後，再查會變 404
def test_delete_own_workspace(client):
    token = _register_and_login(client, "frank@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "要刪的"}, headers=_auth_headers(token)
    ).json()["id"]

    delete_response = client.delete(
        f"/workspaces/{ws_id}", headers=_auth_headers(token)
    )
    assert delete_response.status_code == 204

    get_response = client.get(f"/workspaces/{ws_id}", headers=_auth_headers(token))
    assert get_response.status_code == 404


# 測試：B 不能刪 A 的 workspace
def test_cannot_delete_others_workspace(client):
    token_a = _register_and_login(client, "gina_a@example.com")
    ws_id = client.post(
        "/workspaces", json={"name": "A的研究"}, headers=_auth_headers(token_a)
    ).json()["id"]

    token_b = _register_and_login(client, "gina_b@example.com")
    response = client.delete(f"/workspaces/{ws_id}", headers=_auth_headers(token_b))

    assert response.status_code == 403
