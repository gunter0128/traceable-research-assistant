# 問答的流程：確認 workspace 是你的 → 問題轉向量 → 搜相關 chunk → 組 context 丟給 LLM → 整理成答案 + 引用來源
# 不寫 SQL 不碰 HTTP

import time

from sqlalchemy.orm import Session

from app.core import openai_client
from app.repositories import chunk_repo
from app.schemas.qa import AskResponse, CitationOut
from app.services import workspace_service

# 使用者最多等多久就要看到答案或錯誤訊息
ANSWER_BUDGET_SECONDS = 60


# 問答時 OpenAI 出問題：使用者正在等，不能延後，直接讓 API 回錯誤訊息
# 暫時性的（重試過還是不行）跟永久性的（設定有誤）分開，給使用者的訊息不同
class AIServiceUnavailable(Exception):
    pass


class AIServiceMisconfigured(Exception):
    pass


def ask(db: Session, workspace_id: int, owner_id: int, question: str) -> AskResponse:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)

    # 整個問答共用 60 秒的時間預算（docs/decisions/008）：轉向量跟生成答案各自的重試加起來，也不會超過
    deadline = time.monotonic() + ANSWER_BUDGET_SECONDS
    try:
        query_embedding = openai_client.embed_query(question, deadline=deadline)
        chunks = chunk_repo.search_similar(db, workspace_id, query_embedding)
        answer = openai_client.generate_answer(question, _build_context(chunks), deadline=deadline)
    # openai_client 已經做完秒級重試了，走到這裡代表重試也沒用
    # from None：不把 OpenAI 原本的錯誤接在後面，API 層只需要知道是哪一種
    except openai_client.OpenAITemporaryError:
        raise AIServiceUnavailable from None
    except openai_client.OpenAIPermanentError:
        raise AIServiceMisconfigured from None

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


# 把搜到的幾段內容兜成一段 context 標號只是方便閱讀，不會拿來解析
def _build_context(chunks) -> str:
    pieces = []
    for i, chunk in enumerate(chunks, start=1):
        pieces.append(f"[{i}] {chunk.content}")
    return "\n\n".join(pieces)
