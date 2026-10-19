# /ask 端點的測試：問答 + ownership。真的打 OpenAI 的部分全部 mock 掉，不花錢、結果每次都一樣


from datetime import date
from types import SimpleNamespace

import pytest

from app.core import openai_client, s3_client
from app.services import qa_service
from tests.pdf_fixtures import valid_pdf_bytes
from tests.upload_helpers import upload_pdf


# autouse=True 的意思是：「不用等有人在參數列表裡點名要你，這個檔案裡每一個測試開始前，自動先跑我」
# 所以這個 fixture 不會看到有人呼叫他
@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake_bucket = {}
    monkeypatch.setattr(s3_client, "upload_file", lambda key, content: fake_bucket.__setitem__(key, content))
    monkeypatch.setattr(s3_client, "download_file", lambda key: fake_bucket[key])
    monkeypatch.setattr(s3_client, "delete_file", lambda key: fake_bucket.pop(key, None))


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    # 上傳時文件處理用 embed_texts（一次一批），問問題時用 embed_query（一次一句），兩個都擋掉
    monkeypatch.setattr(openai_client, "embed_texts", lambda texts, **kwargs: [[0.1] * 1536 for _ in texts])
    monkeypatch.setattr(openai_client, "embed_query", lambda text, **kwargs: [0.1] * 1536)


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
    return upload_pdf(client, _auth_headers(token), workspace_id, valid_pdf_bytes(text), "thesis.pdf")


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
    # 把丟給模型的資料記下來，最後檢查裡面有檔名和今天日期
    sent = {}

    def fake_generate(question, context, **kwargs):
        sent["context"] = context
        return "這是生成的答案[1]"

    monkeypatch.setattr(openai_client, "generate_answer", fake_generate)

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
    assert body["answer"] == "這是生成的答案[1]"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["number"] == 1
    assert body["citations"][0]["filename"] == "thesis.pdf"
    assert "Multi-hop retrieval" in body["citations"][0]["content"]
    assert "今天日期" in sent["context"]
    assert "【文件】thesis.pdf" in sent["context"]


# 測試：答案沒有標任何來源（例如說找不到）時，不附引用（docs/decisions/021）
# 以前會把搜到的段落全部當引用，變成「答案說找不到，下面卻附了一份文件」
def test_answer_without_markers_has_no_citations(client, monkeypatch):
    monkeypatch.setattr(
        openai_client, "generate_answer", lambda question, context, **kwargs: "文件中找不到相關資訊"
    )
    token = _register_and_login(client, "erin@example.com")
    ws_id = _create_workspace(client, token)
    _upload_pdf(client, token, ws_id, text="Multi-hop retrieval is the core method")

    response = client.post(
        f"/workspaces/{ws_id}/ask",
        json={"question": "法國的首都是哪裡？"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["citations"] == []


# 測試：模型有回答、卻忘了標編號時，退回列出搜到的段落（安全網，docs/decisions/021）
def test_answer_without_markers_falls_back_to_retrieved_passages(client, monkeypatch):
    monkeypatch.setattr(openai_client, "generate_answer", lambda question, context, **kwargs: "核心方法是多跳檢索")
    token = _register_and_login(client, "frank@example.com")
    ws_id = _create_workspace(client, token)
    _upload_pdf(client, token, ws_id, text="Multi-hop retrieval is the core method")

    response = client.post(
        f"/workspaces/{ws_id}/ask",
        json={"question": "這篇論文的方法是什麼？"},
        headers=_auth_headers(token),
    )

    assert [c["number"] for c in response.json()["citations"]] == [1]


# 測試：從答案裡找出引用標記（docs/decisions/021）
# 各種寫法都認得、統一寫成 [1][2]；超出範圍的編號拿掉；整個都不在範圍內的（可能不是引用）原文不動
@pytest.mark.parametrize(
    ("answer", "expected_answer", "expected_cited"),
    [
        ("14 天[2]。", "14 天[2]。", {2}),
        ("可以[1][3]，也可以［2］", "可以[1][3]，也可以[2]", {1, 2, 3}),
        ("見[1, 2]、[2，3]", "見[1][2]、[2][3]", {1, 2, 3}),
        ("見[1, 9]", "見[1]", {1}),
        ("民國[2025]年", "民國[2025]年", set()),
        ("文件中找不到相關資訊", "文件中找不到相關資訊", set()),
    ],
)
def test_extract_citations(answer, expected_answer, expected_cited):
    assert qa_service._extract_citations(answer, 3) == (expected_answer, expected_cited)


# 測試：給模型的資料（docs/decisions/018）
# 同一份文件的段落放在一起、照原文順序排；每份文件附上檔名和開頭；編號跨文件連續；最前面是今天日期
# 用 SimpleNamespace 假造段落和文件：這裡只測「怎麼兜」，不需要真的資料庫
def test_context_groups_chunks_by_document_with_header_and_date():
    new = SimpleNamespace(id=1, filename="手冊_2026.pdf")
    old = SimpleNamespace(id=2, filename="手冊_2025.pdf")
    # 搜尋結果的順序：新版第 3 段最相關，其次是舊版第 1 段，再來是新版第 1 段
    chunks = [
        SimpleNamespace(document_id=1, document=new, chunk_index=3, content="新版第 3 段"),
        SimpleNamespace(document_id=2, document=old, chunk_index=1, content="舊版第 1 段"),
        SimpleNamespace(document_id=1, document=new, chunk_index=1, content="新版第 1 段"),
    ]
    headers = {1: "員工手冊（2026 年版）\n生效日期：2026 年 1 月 1 日", 2: "員工手冊（2025 年版）"}

    groups = qa_service._group_by_document(chunks)
    context = qa_service._build_context(groups, headers, date(2026, 10, 20))

    assert context == (
        "今天日期：2026 年 10 月 20 日\n\n"
        "【文件】手冊_2026.pdf\n文件開頭：員工手冊（2026 年版） 生效日期：2026 年 1 月 1 日\n\n"
        "[1] 新版第 1 段\n\n"
        "[2] 新版第 3 段\n\n"
        "【文件】手冊_2025.pdf\n文件開頭：員工手冊（2025 年版）\n\n"
        "[3] 舊版第 1 段"
    )


# 測試：文件開頭只取前 200 個字，很長的第一段不會整段塞進去
def test_context_header_is_truncated():
    doc = SimpleNamespace(id=1, filename="a.pdf")
    chunk = SimpleNamespace(document_id=1, document=doc, chunk_index=5, content="內容")

    context = qa_service._build_context(qa_service._group_by_document([chunk]), {1: "字" * 500}, date(2026, 1, 1))

    assert "文件開頭：" + "字" * qa_service.DOCUMENT_HEADER_CHARS + "\n" in context
    assert "字" * (qa_service.DOCUMENT_HEADER_CHARS + 1) not in context


# 測試：workspace 裡還沒有任何文件時問問題，不會噴錯，只是 citations 是空的
def test_ask_with_no_documents_returns_empty_citations(client, monkeypatch):
    monkeypatch.setattr(
        openai_client, "generate_answer", lambda question, context, **kwargs: "文件中找不到相關資訊"
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
