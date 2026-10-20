# 用量記錄和每日上限（docs/decisions/012）
# 測的是：每次呼叫 OpenAI 都記下花了多少；開始之前沒到上限就放行、而且完整做完；
# 跨過上限之後馬上查得到；下一次才擋；全系統的上限擋得住大量註冊的帳號

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core import openai_client, s3_client
from app.core.openai_client import Usage
from app.models.document import Document
from app.models.usage_record import UsageRecord
from app.models.user import User
from app.services import usage_service
from tests.pdf_fixtures import valid_pdf_bytes
from tests.upload_helpers import upload_pdf


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))

    # 假的 OpenAI：跟真的一樣，成功之後把用了多少交給 on_usage
    def embed_texts(texts, on_attempt=None, on_usage=None):
        if on_usage:
            on_usage(Usage(openai_client.EMBEDDING_MODEL, 1000 * len(texts)))
        return [[0.1] * 1536 for _ in texts]

    def embed_query(text, deadline=None, on_usage=None):
        if on_usage:
            on_usage(Usage(openai_client.EMBEDDING_MODEL, 20))
        return [0.1] * 1536

    def generate_answer(question, context, deadline=None, on_usage=None):
        if on_usage:
            on_usage(Usage(openai_client.GENERATION_MODEL, 2500, 300))
        return "答案"

    monkeypatch.setattr(openai_client, "embed_texts", embed_texts)
    monkeypatch.setattr(openai_client, "embed_query", embed_query)
    monkeypatch.setattr(openai_client, "generate_answer", generate_answer)


def _login(client, email):
    client.post("/auth/register", json={"email": email, "password": "pw123456"})
    token = client.post("/auth/login", json={"email": email, "password": "pw123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _workspace(client, headers):
    return client.post("/workspaces", json={"name": "研究"}, headers=headers).json()["id"]


def _upload(client, headers, ws_id):
    return upload_pdf(client, headers, ws_id, valid_pdf_bytes("retrieval " * 50), "a.pdf")


def _ask(client, headers, ws_id):
    return client.post(f"/workspaces/{ws_id}/ask", json={"question": "問題"}, headers=headers)


def _user_id(db_session, email):
    return db_session.query(User).filter(User.email == email).one().id


# 直接塞一筆用量，模擬「今天已經用了多少」
def _spend(db_session, user_id, cost, created_at=None):
    record = UsageRecord(user_id=user_id, purpose="qa", model="x", input_tokens=0, cost_usd=Decimal(cost))
    if created_at is not None:
        record.created_at = created_at
    db_session.add(record)
    db_session.commit()


# 測試：價格換算。gpt-4o 輸入 100 萬個 token 2.50、輸出 100 萬個 10
def test_cost_calculation():
    assert Usage(openai_client.GENERATION_MODEL, 1_000_000, 1_000_000).cost_usd() == Decimal("12.50")
    assert Usage(openai_client.EMBEDDING_MODEL, 1_000_000).cost_usd() == Decimal("0.02")


# 測試：問一次問題，記下兩筆（問題轉向量、生成答案），金額正確
def test_ask_records_usage(client, db_session):
    headers = _login(client, "alice@example.com")
    ws_id = _workspace(client, headers)

    assert _ask(client, headers, ws_id).status_code == 200

    records = db_session.query(UsageRecord).filter(UsageRecord.purpose == "qa").all()
    assert sorted(r.model for r in records) == sorted([openai_client.EMBEDDING_MODEL, openai_client.GENERATION_MODEL])
    generate = next(r for r in records if r.model == openai_client.GENERATION_MODEL)
    assert (generate.input_tokens, generate.output_tokens) == (2500, 300)
    assert generate.cost_usd == Decimal("0.00925")


# 測試：處理文件的用量算在上傳的人身上，記下是哪份文件
def test_processing_records_usage_for_owner(client, db_session):
    headers = _login(client, "bob@example.com")
    ws_id = _workspace(client, headers)

    document_id = _upload(client, headers, ws_id).json()["id"]

    record = db_session.query(UsageRecord).filter(UsageRecord.purpose == "document").one()
    assert record.user_id == _user_id(db_session, "bob@example.com")
    assert record.document_id == document_id


# 測試：生成答案失敗，問題轉向量已經花掉的錢還是要記
def test_failed_ask_still_records_spent_usage(client, db_session, monkeypatch):
    def broken(question, context, deadline=None, on_usage=None):
        raise openai_client.OpenAITemporaryError("down")

    monkeypatch.setattr(openai_client, "generate_answer", broken)
    headers = _login(client, "carol@example.com")
    ws_id = _workspace(client, headers)

    assert _ask(client, headers, ws_id).status_code == 503

    records = db_session.query(UsageRecord).all()
    assert [r.model for r in records] == [openai_client.EMBEDDING_MODEL]


# 測試：開始之前還沒到上限 → 放行、完整回答；回答完已經超過 → 馬上查得到；下一次才擋
def test_crossing_the_limit_finishes_then_blocks(client, db_session):
    headers = _login(client, "dave@example.com")
    ws_id = _workspace(client, headers)
    _spend(db_session, _user_id(db_session, "dave@example.com"), "0.4999")

    first = _ask(client, headers, ws_id)
    usage = client.get("/usage/today", headers=headers).json()
    second = _ask(client, headers, ws_id)

    assert first.status_code == 200
    assert first.json()["answer"] == "答案"
    assert usage["limit_reached"] is True
    assert second.status_code == 429
    assert second.json()["detail"] == "今日用量已達上限，明天再試"


# 測試：到上限之後，上傳和重試也擋
def test_limit_blocks_upload_and_retry(client, db_session):
    headers = _login(client, "erin@example.com")
    ws_id = _workspace(client, headers)
    document_id = _upload(client, headers, ws_id).json()["id"]
    db_session.get(Document, document_id).status = "failed"  # 做一份可以重試的文件
    db_session.commit()
    _spend(db_session, _user_id(db_session, "erin@example.com"), "0.5")

    assert _upload(client, headers, ws_id).status_code == 429
    assert client.post(f"/documents/{document_id}/retry", headers=headers).status_code == 429


# 測試：全系統的上限：別人（或很多帳號）把今天的額度用完了，其他人也不能用，訊息不一樣
def test_system_limit_blocks_everyone(client, db_session):
    _login(client, "spammer@example.com")
    _spend(db_session, _user_id(db_session, "spammer@example.com"), "5")
    headers = _login(client, "frank@example.com")
    ws_id = _workspace(client, headers)

    response = _ask(client, headers, ws_id)

    assert response.status_code == 429
    assert response.json()["detail"] == "系統今日用量已達上限，明天再試"
    assert client.get("/usage/today", headers=headers).json()["limit_reached"] is True


# 測試：「今天」用台灣時間算，昨天（台灣時間）用的不算
def test_yesterday_does_not_count(client, db_session):
    headers = _login(client, "grace@example.com")
    ws_id = _workspace(client, headers)
    yesterday = datetime.now(usage_service.TAIWAN) - timedelta(days=1)
    _spend(db_session, _user_id(db_session, "grace@example.com"), "10", created_at=yesterday)

    assert _ask(client, headers, ws_id).status_code == 200
    assert client.get("/usage/today", headers=headers).json()["limit_reached"] is False
