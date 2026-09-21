# chunk_repo.search_similar() 的測試：只測這個 repository 函式本身
# 直接用 db_session 組資料（不透過 API 上傳），才能精準控制每個 chunk 的向量是什麼

from app.models.chunk import Chunk
from app.models.document import Document
from app.repositories import chunk_repo


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


def _add_document(db_session, workspace_id, filename="doc.pdf"):
    document = Document(workspace_id=workspace_id, filename=filename, storage_path="x")
    db_session.add(document)
    db_session.commit()
    db_session.refresh(document)
    return document


def _add_chunk(db_session, document_id, content, embedding):
    chunk = Chunk(
        document_id=document_id, chunk_index=0, content=content, embedding=embedding
    )
    db_session.add(chunk)
    db_session.commit()
    return chunk


# 1536 維向量：QUERY 跟 CLOSE 指同一個方向（距離 0，最像），FAR 指另一個方向（距離較大）
QUERY = [1.0] + [0.0] * 1535
CLOSE = [1.0] + [0.0] * 1535
FAR = [0.0, 1.0] + [0.0] * 1534


# 測試：只搜得到指定 workspace 底下的 chunk，搜不到別的 workspace 的（即使都是同一個使用者的）
def test_search_similar_scoped_to_workspace(client, db_session):
    token = _register_and_login(client, "alice@example.com")
    ws_a = _create_workspace(client, token, "workspace A")
    ws_b = _create_workspace(client, token, "workspace B")

    doc_a = _add_document(db_session, ws_a)
    doc_b = _add_document(db_session, ws_b)
    _add_chunk(db_session, doc_a.id, "來自 A 的內容", CLOSE)
    _add_chunk(db_session, doc_b.id, "來自 B 的內容", CLOSE)

    results = chunk_repo.search_similar(db_session, ws_a, QUERY)

    assert len(results) == 1
    assert results[0].content == "來自 A 的內容"


# 測試：依相似度排序，越像 query 向量的排越前面
def test_search_similar_orders_by_distance(client, db_session):
    token = _register_and_login(client, "bob@example.com")
    ws_id = _create_workspace(client, token)
    doc = _add_document(db_session, ws_id)

    # 故意先存比較不像的，確認排序靠的是向量距離，不是存入順序
    _add_chunk(db_session, doc.id, "比較不像", FAR)
    _add_chunk(db_session, doc.id, "比較像", CLOSE)

    results = chunk_repo.search_similar(db_session, ws_id, QUERY)

    assert [r.content for r in results] == ["比較像", "比較不像"]


# 測試：limit 參數真的有限制筆數
def test_search_similar_respects_limit(client, db_session):
    token = _register_and_login(client, "erin@example.com")
    ws_id = _create_workspace(client, token)
    doc = _add_document(db_session, ws_id)

    for i in range(5):
        _add_chunk(db_session, doc.id, f"第 {i} 段", CLOSE)

    results = chunk_repo.search_similar(db_session, ws_id, QUERY, limit=3)

    assert len(results) == 3
