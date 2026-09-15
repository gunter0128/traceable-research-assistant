# auth 的測試：register / login / me

# 每個測試三段（AAA）測試撰寫慣例：
# Arrange ：把系統弄到需要的狀態(不是在測這個 可省略)。
# Act     ：真正要驗證行為的那一個呼叫 存成 response
# Assert  ：檢查 response 對不對


# 測試：註冊成功會回 201，而且回應裡有 id、email，但不會外洩密碼
def test_register_success(client):
    # Arrange
    payload = {"email": "alice@example.com", "password": "pw123456"}

    # Act
    response = client.post("/auth/register", json=payload)

    # Assert
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert "password" not in body  # 密碼(雜湊)不該外洩
    assert "id" in body


# 測試：同一個 email 註冊兩次，第二次要回 409
def test_register_duplicate_email(client):
    payload = {"email": "bob@example.com", "password": "pw123456"}
    client.post("/auth/register", json=payload)  # Arrange：讓這 email 已存在

    response = client.post("/auth/register", json=payload)  # Act：再註冊一次

    assert response.status_code == 409


# 測試：帳密都對，登入成功會拿到 token
def test_login_success(client):
    client.post(  # Arrange
        "/auth/register", json={"email": "carol@example.com", "password": "pw123456"}
    )

    response = client.post(  # Act
        "/auth/login", json={"email": "carol@example.com", "password": "pw123456"}
    )

    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


# 測試：email 對、密碼錯，登入要回 401
def test_login_wrong_password(client):
    client.post(  # Arrange
        "/auth/register", json={"email": "dave@example.com", "password": "pw123456"}
    )

    response = client.post(  # Act：錯的密碼
        "/auth/login", json={"email": "dave@example.com", "password": "wrong"}
    )

    assert response.status_code == 401


# 測試：帶著登入拿到的 token 打 /me，可以拿回自己的資料
def test_me_with_valid_token(client):
    client.post(
        "/auth/register", json={"email": "erin@example.com", "password": "pw123456"}
    )
    login_response = client.post(
        "/auth/login", json={"email": "erin@example.com", "password": "pw123456"}
    )
    token = login_response.json()["access_token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "erin@example.com"


# 測試：沒帶 token 打 /me，要被擋 401
def test_me_without_token(client):
    response = client.get("/auth/me")

    assert response.status_code == 401
