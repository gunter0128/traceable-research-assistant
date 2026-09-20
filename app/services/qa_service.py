# 問答的流程：確認 workspace 是你的 → 問題轉向量 → 搜相關 chunk → 組 context 丟給 LLM → 整理成答案 + 引用來源
# 不寫 SQL 不碰 HTTP

from sqlalchemy.orm import Session

from app.core import openai_client
from app.repositories import chunk_repo
from app.schemas.qa import AskResponse, CitationOut
from app.services import workspace_service


def ask(db: Session, workspace_id: int, owner_id: int, question: str) -> AskResponse:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)

    query_embedding = openai_client.embed_text(question)
    chunks = chunk_repo.search_similar(db, workspace_id, query_embedding)

    # 把搜到的幾段內容兜成一段 context 標號只是方便閱讀，不會拿來解析
    pieces = []
    for i, chunk in enumerate(chunks, start=1):
        pieces.append(f"[{i}] {chunk.content}")
    context = "\n\n".join(pieces)

    answer = openai_client.generate_answer(question, context)

    # 簡單版 citation：這次搜到、丟給 LLM 的 chunk 全部算數 不檢查答案有沒有每段都真的用到
    citations = []
    for chunk in chunks:
        citations.append(
            CitationOut(
                document_id=chunk.document_id,
                filename=chunk.document.filename,
                content=chunk.content,
            )
        )

    return AskResponse(answer=answer, citations=citations)
