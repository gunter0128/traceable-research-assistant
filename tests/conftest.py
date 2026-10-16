# pytest 自動載入這個檔（名字固定叫 conftest.py）裡面的 fixture 不用 import
# 所有 test_*.py 都能用 這裡提供 db_session / client 兩個 fixture
# 在 import app 的任何東西之前，先把 DATABASE_URL 換成測試分支
# 這樣 app.core.config 的 settings 讀到的才是測試資料庫，不是正式的 Neon

import os

from dotenv import dotenv_values

# dotenv_values 只是讀檔案回傳一個 dict 不會自動生效
_test_env = dotenv_values(".env.test")
# os.environ 是代表這個程式環境變數的特殊 dict 寫進去 = 真的設一個環境變數
# pydantic-settings 的規則：環境變數優先權比 .env 檔案高 所以等一下 Settings() 會撿到這裡設的值 不是 .env 裡的
os.environ["DATABASE_URL"] = _test_env["DATABASE_URL"]
os.environ["SECRET_KEY"] = _test_env["SECRET_KEY"]

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base, get_db
from app.main import app

engine = create_engine(os.environ["DATABASE_URL"])
TestSessionLocal = sessionmaker(bind=engine)


# fixture：一個"準備測試需要的東西"的函式，pytest 自動幫你呼叫
# 測試函式只要把參數命名為 db_session / client，pytest 就會跑對應的 fixture
# 把 yield 出去的東西當作那個參數傳進測試（跟 FastAPI 的 Depends 同一個想法）
# 有 yield 的 fixture 分兩半跑：yield 之前 = 每個測試前的前置，yield 之後 = 每個測試後的收尾


@pytest.fixture
def db_session():
    Base.metadata.create_all(engine)  # 前置：照 Base 藍圖建空表
    session = TestSessionLocal()
    try:
        yield session  # 造一個 session、交給測試，暫停在這等測試跑完
    finally:
        session.close()
        Base.metadata.drop_all(engine)  # 收尾：把表砍掉，下個測試從零開始


# client 依賴 db_session（參數寫 db_session，pytest 會先幫你把它生出來）
# 跟當初 auth_client(client) 依賴 client 是同一招：fixture 可以疊 fixture
@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db  # 前置：app 遇到 get_db 就改用這個 → endpoint 全連測試資料庫
    yield TestClient(app)  # 造一個 TestClient(FastAPI提供的工具 模擬真實HTTP請求)交給測試
    app.dependency_overrides.clear()  # 收尾：移除上面的替換，app 恢復正常


# 上傳改成瀏覽器直接傳到 S3（docs/decisions/013），測試裡用假的取代兩個 S3 呼叫，所有測試都自動套用
#   create_upload_form：假的上傳許可（真的那個只在本機計算，但需要 AWS 憑證）
#   file_size：去各測試自己的假 bucket 查檔案大小。寫成 s3_client.download_file(key)
#     而不是直接拿某個 dict，是因為每個測試檔的假 bucket 都不一樣，這樣會用到「當下被換掉的那個」
@pytest.fixture(autouse=True)
def _fake_upload_form(monkeypatch):
    from app.core import s3_client

    def file_size(key):
        try:
            return len(s3_client.download_file(key))
        except KeyError:
            raise s3_client.FileMissing("404") from None

    monkeypatch.setattr(
        s3_client, "create_upload_form",
        lambda key, max_bytes, expires_seconds: {"url": "https://fake-bucket", "fields": {"key": key}},
    )
    monkeypatch.setattr(s3_client, "file_size", file_size)
