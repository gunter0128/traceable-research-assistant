# 問答的 OpenAI 呼叫：openai_client.embed_query、generate_answer，以及 qa_service 怎麼用它們（docs/decisions/046 B3-2）
# 測的是：
#   回傳 (結果, 用量)；雲端（OpenAI）和地端（自己架的服務）走同一段程式
#   每次請求的等待上限；不自動重試（使用者在等，失敗了直接回 503）
#   整個 /ask：送給模型的內容、答案和引用、用量都跟改之前一樣
#   整個問答 50 秒的預算：生成答案最多等「ANSWER_TIMEOUT 和剩下的時間，取小的」
# 用真的 OpenAI SDK，只把網路換成假的（helpers.use_fake_openai），不花錢

from decimal import Decimal

import httpx2
import pytest

from app.core import openai_client
from app.models.usage_record import UsageRecord
from app.services import qa_service
from tests.helpers import (
    create_workspace,
    fake_answer,
    fake_embed_query,
    fake_embed_texts,
    login_headers,
    openai_error,
    upload_pdf,
    use_fake_openai,
)
from tests.pdf_fixtures import valid_pdf_bytes

CLOUD_URL = "https://api.openai.com/v1"
LOCAL_URL = "http://gpu-server:8000/v1"  # 企業內部版自己架的服務（vLLM，docs/decisions/037）


# 問題轉向量成功的回應：1536 維（跟測試資料庫的向量欄位一樣），用了 5 個 token
def _embedding_ok():
    return httpx2.Response(200, json={
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * 1536}],
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    })


# 生成答案成功的回應：送進去 100 個 token、生成 20 個
def _answer_ok(content="這是生成的答案[1]"):
    return httpx2.Response(200, json={
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


def _connection_error():
    return httpx2.ConnectError("連不上", request=httpx2.Request("POST", CLOUD_URL))


def _read_timeout():
    return httpx2.ReadTimeout("等太久", request=httpx2.Request("POST", CLOUD_URL))


# 每一次請求的等待上限（httpx 分成連線、讀取、寫入、等連線池四種，都設成同一個數字）
def _timeout_of(request):
    return request.extensions["timeout"]["read"]


# ---------- 問題轉向量、生成答案：各自的行為（不用資料庫）----------


# 測試：問題轉向量回傳 (向量, 用量)，問題前面加上設定的說明，每次請求最多等 QA_TIMEOUT 秒
@pytest.mark.parametrize("base_url", [CLOUD_URL, LOCAL_URL])
def test_embed_query_returns_vector_and_usage(monkeypatch, base_url):
    server = use_fake_openai(monkeypatch, "embedding_client", _embedding_ok(), base_url=base_url)
    monkeypatch.setattr(openai_client.settings, "embedding_query_prefix", "Query:")

    vector, usage = openai_client.embed_query("特休幾天？")

    assert vector == [0.1] * 1536
    assert usage == openai_client.Usage(openai_client.EMBEDDING_MODEL, input_tokens=5)
    assert str(server.requests[0].url) == f"{base_url}/embeddings"
    assert server.sent()["input"] == ["Query:特休幾天？"]
    assert _timeout_of(server.requests[0]) == openai_client.QA_TIMEOUT


# 測試：生成答案回傳 (答案, 用量)，送出回答原則和資料、額外參數（地端關掉思考模式），等待上限用呼叫的人給的
@pytest.mark.parametrize(
    ("base_url", "extra_body"),
    [(CLOUD_URL, None), (LOCAL_URL, {"chat_template_kwargs": {"enable_thinking": False}})],
)
def test_generate_answer_returns_answer_and_usage(monkeypatch, base_url, extra_body):
    server = use_fake_openai(monkeypatch, "answer_client", _answer_ok(), base_url=base_url)
    monkeypatch.setattr(openai_client, "answer_extra_body", extra_body)

    answer, usage = openai_client.generate_answer("回答原則", "資料和問題", timeout=12)

    assert answer == "這是生成的答案[1]"
    assert usage == openai_client.Usage(openai_client.GENERATION_MODEL, 100, 20)
    sent = server.sent()
    assert str(server.requests[0].url) == f"{base_url}/chat/completions"
    assert sent["model"] == openai_client.GENERATION_MODEL
    assert sent["messages"] == [{"role": "system", "content": "回答原則"}, {"role": "user", "content": "資料和問題"}]
    # extra_body 的欄位會直接放進送出去的 JSON 裡；沒設定就什麼都不多送
    assert sent.get("chat_template_kwargs") == (extra_body or {}).get("chat_template_kwargs")
    assert _timeout_of(server.requests[0]) == 12


# 測試：開源模型想到長度上限還沒想完，答案是空的（content 是 None）→ 當成空字串，用量照樣回傳
def test_generate_answer_without_content_is_empty(monkeypatch):
    use_fake_openai(monkeypatch, "answer_client", _answer_ok(content=None))

    answer, usage = openai_client.generate_answer("回答原則", "資料和問題", timeout=12)

    assert answer == ""
    assert usage.output_tokens == 20


def _embed():
    return openai_client.embed_query("問題")


def _generate():
    return openai_client.generate_answer("回答原則", "資料和問題", timeout=12)


# (換掉哪個 client, 怎麼呼叫)：問題轉向量、生成答案兩種
CALLS = [pytest.param("embedding_client", _embed, id="embed_query"), pytest.param("answer_client", _generate, id="generate_answer")]


# 測試：暫時性的錯誤 → 不重試、不等，送一次就丟暫時性錯誤（API 回 503「AI 服務暫時無法使用」）
# 對照文件處理（test_embed_texts.py）：同樣的錯誤，SDK 會重試 2 次
# 429 加上「請等 1 秒」也不等：使用者在等，不讓 SDK 照 OpenAI 說的等（最多會等到 120 秒）
@pytest.mark.parametrize(("client_name", "call"), CALLS)
@pytest.mark.parametrize(
    "failure",
    [
        openai_error(500),
        openai_error(503),
        openai_error(429, headers={"retry-after": "1"}),
        _connection_error(),
        _read_timeout(),
    ],
    ids=["500", "503", "429-retry-after", "connection", "timeout"],
)
def test_qa_calls_are_not_retried(monkeypatch, sdk_sleeps, client_name, call, failure):
    server = use_fake_openai(monkeypatch, client_name, failure)

    with pytest.raises(openai_client.OpenAITemporaryError):
        call()

    assert len(server.requests) == 1
    assert sdk_sleeps == []


# 測試：永久性錯誤（金鑰無效、餘額不足）→ 送一次就丟永久性錯誤（API 回 503「系統設定有誤」、告警）
# 不重試，所以餘額不足也不會像文件處理那樣先白試 2 次
@pytest.mark.parametrize(("client_name", "call"), CALLS)
@pytest.mark.parametrize(
    "failure",
    [openai_error(401), openai_error(429, code="insufficient_quota")],
    ids=["401", "insufficient_quota"],
)
def test_qa_permanent_errors(monkeypatch, sdk_sleeps, client_name, call, failure):
    server = use_fake_openai(monkeypatch, client_name, failure)

    with pytest.raises(openai_client.OpenAIPermanentError):
        call()

    assert len(server.requests) == 1


# ---------- 整個 /ask（用資料庫）----------


# 上傳一份文件（文件處理的轉向量換成假的函式），回傳 headers、workspace 編號
def _workspace_with_document(client, monkeypatch, email):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)
    headers = login_headers(client, email)
    ws_id = create_workspace(client, headers)
    upload = upload_pdf(client, headers, ws_id, valid_pdf_bytes("Multi-hop retrieval is the core method"), "thesis.pdf")
    assert upload.json()["status"] == "processed"
    return headers, ws_id


def _ask(client, headers, ws_id, question="這篇論文的方法是什麼？"):
    return client.post(f"/workspaces/{ws_id}/ask", json={"question": question}, headers=headers)


def _qa_usage(db_session):
    return db_session.query(UsageRecord).filter(UsageRecord.purpose == "qa").order_by(UsageRecord.id).all()


# 測試：正常問答，整條路都用真的 SDK
#   送給模型的回答原則、資料（檔名、今天日期、段落編號、引用提醒）跟改之前一樣
#   答案和引用照舊；兩次呼叫各送一次；用量照回應記下兩筆（轉向量 5 個 token、生成 100 + 20 個）
#   等待上限：轉向量 QA_TIMEOUT、生成 ANSWER_TIMEOUT（預算還很多，用不到「剩下的時間」）
def test_ask_end_to_end(client, db_session, monkeypatch, sdk_sleeps):
    # 這裡不測時間預算（下面另外用假的時鐘測）：預算放大，遠端的測試資料庫偶爾很慢時，結果才不會跟著變
    # （B3-3 實際遇過：搜尋卡了 88 秒，預算用完，生成答案沒送出去）
    monkeypatch.setattr(qa_service, "ANSWER_BUDGET_SECONDS", 10_000)
    headers, ws_id = _workspace_with_document(client, monkeypatch, "alice@example.com")
    embedding_server = use_fake_openai(monkeypatch, "embedding_client", _embedding_ok())
    answer_server = use_fake_openai(monkeypatch, "answer_client", _answer_ok())

    response = _ask(client, headers, ws_id)

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "這是生成的答案[1]"
    assert [(c["number"], c["filename"]) for c in body["citations"]] == [(1, "thesis.pdf")]
    assert "Multi-hop retrieval" in body["citations"][0]["content"]

    messages = answer_server.sent()["messages"]
    assert messages[0] == {"role": "system", "content": qa_service.ANSWER_RULES}
    assert messages[1]["content"].startswith("資料：\n今天日期：")
    assert "【文件】thesis.pdf" in messages[1]["content"]
    assert "[1] Multi-hop retrieval is the core method" in messages[1]["content"]
    assert messages[1]["content"].endswith(f"問題：這篇論文的方法是什麼？\n\n{qa_service.CITATION_REMINDER}")

    assert len(embedding_server.requests) == 1
    assert len(answer_server.requests) == 1
    assert sdk_sleeps == []
    assert _timeout_of(embedding_server.requests[0]) == openai_client.QA_TIMEOUT
    assert _timeout_of(answer_server.requests[0]) == openai_client.ANSWER_TIMEOUT

    records = _qa_usage(db_session)
    assert [(r.model, r.input_tokens, r.output_tokens) for r in records] == [
        (openai_client.EMBEDDING_MODEL, 5, 0),
        (openai_client.GENERATION_MODEL, 100, 20),
    ]
    assert records[1].cost_usd == Decimal("0.00045")  # gpt-4o：100 × 2.50 / 100 萬 + 20 × 10 / 100 萬


# 測試：生成答案時 OpenAI 暫時出問題（503、超時）→ 回 503、只送一次；轉向量已經花掉的錢照樣記
@pytest.mark.parametrize("failure", [openai_error(503), _read_timeout()], ids=["503", "timeout"])
def test_ask_answer_failure_returns_503_and_keeps_embedding_usage(client, db_session, monkeypatch, sdk_sleeps, failure):
    monkeypatch.setattr(qa_service, "ANSWER_BUDGET_SECONDS", 10_000)  # 理由同 test_ask_end_to_end
    headers, ws_id = _workspace_with_document(client, monkeypatch, "bob@example.com")
    use_fake_openai(monkeypatch, "embedding_client", _embedding_ok())
    answer_server = use_fake_openai(monkeypatch, "answer_client", failure)

    response = _ask(client, headers, ws_id)

    assert response.status_code == 503
    assert response.json()["detail"] == "AI 服務暫時無法使用，請稍後再試"
    assert len(answer_server.requests) == 1
    assert sdk_sleeps == []
    assert [r.model for r in _qa_usage(db_session)] == [openai_client.EMBEDDING_MODEL]


# ---------- 整個問答的時間預算 ----------
# 真的時鐘換成假的：qa_service 看的「現在幾秒」由測試決定，不用真的等


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


# 換掉 qa_service 看的時鐘，問題轉向量一次要「花」seconds 秒；生成答案記下它拿到的等待上限
# 只換 qa_service 自己的 time（monkeypatch 它 import 進來的模組名稱），其他程式的時鐘不受影響
def _slow_embedding_and_recorded_timeout(monkeypatch, seconds):
    clock = FakeClock()
    monkeypatch.setattr(qa_service, "time", clock)
    timeouts = []

    def slow_embed_query(text):
        clock.now += seconds
        return fake_embed_query(text)

    def record_timeout(system, user, timeout):
        timeouts.append(timeout)
        return fake_answer("文件中找不到相關資訊")

    monkeypatch.setattr(openai_client, "embed_query", slow_embed_query)
    monkeypatch.setattr(openai_client, "generate_answer", record_timeout)
    return timeouts


# 測試：預算還夠 → 生成答案等滿 ANSWER_TIMEOUT
def test_answer_waits_full_answer_timeout_when_budget_allows(client, monkeypatch):
    timeouts = _slow_embedding_and_recorded_timeout(monkeypatch, seconds=1)
    headers = login_headers(client, "carol@example.com")

    assert _ask(client, headers, create_workspace(client, headers)).status_code == 200
    assert timeouts == [openai_client.ANSWER_TIMEOUT]


# 測試：地端的設定（生成最多等 45 秒），轉向量花了 10 秒 → 生成答案只能等剩下的 40 秒，整個問答不超過 50 秒
def test_answer_timeout_is_capped_by_remaining_budget(client, monkeypatch):
    monkeypatch.setattr(openai_client, "ANSWER_TIMEOUT", 45)
    timeouts = _slow_embedding_and_recorded_timeout(monkeypatch, seconds=10)
    headers = login_headers(client, "dave@example.com")

    assert _ask(client, headers, create_workspace(client, headers)).status_code == 200
    assert timeouts == [qa_service.ANSWER_BUDGET_SECONDS - 10]


# 測試：前面就把預算用完了（剩不到 1 秒）→ 不送生成答案的請求，回 503；轉向量的用量照樣記
def test_budget_used_up_skips_answer(client, db_session, monkeypatch):
    timeouts = _slow_embedding_and_recorded_timeout(monkeypatch, seconds=qa_service.ANSWER_BUDGET_SECONDS - 0.5)
    headers = login_headers(client, "erin@example.com")

    response = _ask(client, headers, create_workspace(client, headers))

    assert response.status_code == 503
    assert response.json()["detail"] == "AI 服務暫時無法使用，請稍後再試"
    assert timeouts == []
    assert [r.model for r in _qa_usage(db_session)] == [openai_client.EMBEDDING_MODEL]
