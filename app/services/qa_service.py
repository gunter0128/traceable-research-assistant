# 問答的流程：確認 workspace 是你的 → 問題轉向量 → 搜相關 chunk → 組 context 丟給 LLM → 整理成答案 + 引用來源
# 不寫 SQL 不碰 HTTP

import logging
import time

from sqlalchemy.orm import Session

from app.core import log, openai_client
from app.repositories import chunk_repo
from app.schemas.qa import AskResponse, CitationOut
from app.services import usage_service, workspace_service

# 使用者最多等多久就要看到答案或錯誤訊息
# 原本 60 秒，改成 50 秒（docs/decisions/013）：CloudFront 等後端回應最多 60 秒，超過就直接回錯誤給使用者，
# 而 Lambda 冷啟動還要再加幾秒，留 10 秒的餘裕
ANSWER_BUDGET_SECONDS = 50


# 問答時 OpenAI 出問題：使用者正在等，不能延後，直接讓 API 回錯誤訊息
# 暫時性的（重試過還是不行）跟永久性的（設定有誤）分開，給使用者的訊息不同
class AIServiceUnavailable(Exception):
    pass


class AIServiceMisconfigured(Exception):
    pass


def ask(db: Session, workspace_id: int, owner_id: int, question: str) -> AskResponse:
    # 借用 workspace 的 ownership 檢查：先確認這個 workspace 是不是你的
    workspace_service.get_owned_workspace(db, workspace_id, owner_id)
    # 今天的用量到上限了就不回答（docs/decisions/012）。還沒到的話，這次會完整回答完，就算答完之後超過了也一樣
    usage_service.ensure_within_quota(db, owner_id)

    # 每次呼叫 OpenAI 成功，先把用量收集起來，最後再一起寫進資料庫（見下面的 finally）
    # 不在中途寫：寫入要 commit，commit 會讓已經查出來的段落資料失效，後面組引用時又要重查一次
    usages = []
    record_usage = usages.append

    # 整個問答共用 50 秒的時間預算（docs/decisions/008）：轉向量跟生成答案各自的重試加起來，也不會超過
    deadline = time.monotonic() + ANSWER_BUDGET_SECONDS
    # 每一段各記花多久，回答「問答慢在哪」（docs/decisions/010）
    # 問題只記字數、不記內容：使用者的問題可能是未發表的研究
    try:
        with log.timed("qa_embed", question_chars=len(question)):
            query_embedding = openai_client.embed_query(question, deadline=deadline, on_usage=record_usage)
        with log.timed("qa_search") as fields:
            chunks = chunk_repo.search_similar(db, workspace_id, query_embedding)
            fields["chunks"] = len(chunks)
        with log.timed("qa_generate") as fields:
            answer = openai_client.generate_answer(
                question, _build_context(chunks), deadline=deadline, on_usage=record_usage
            )
            fields["answer_chars"] = len(answer or "")
        # 簡單版 citation：這次搜到、丟給 LLM 的 chunk 全部算數 不檢查答案有沒有每段都真的用到
        # 放在 try 裡面：要在下面 finally 寫入用量（commit）之前讀完段落的內容
        citations = [
            CitationOut(document_id=chunk.document_id, filename=chunk.document.filename, content=chunk.content)
            for chunk in chunks
        ]
    # openai_client 已經做完秒級重試了，走到這裡代表重試也沒用
    # from None：不把 OpenAI 原本的錯誤接在後面，API 層只需要知道是哪一種
    except openai_client.OpenAITemporaryError:
        log.event("qa_failed", logging.WARNING, error_type="temporary")
        raise AIServiceUnavailable from None
    except openai_client.OpenAIPermanentError:
        log.event("qa_failed", logging.ERROR, error_type="permanent")
        raise AIServiceMisconfigured from None
    # finally：不管成功或失敗都會執行。轉向量成功、生成答案失敗的話，轉向量的錢還是花掉了，要記
    finally:
        for usage in usages:
            usage_service.record(db, owner_id, "qa", usage)

    return AskResponse(answer=answer, citations=citations)


# 把搜到的幾段內容兜成一段 context 標號只是方便閱讀，不會拿來解析
def _build_context(chunks) -> str:
    pieces = []
    for i, chunk in enumerate(chunks, start=1):
        pieces.append(f"[{i}] {chunk.content}")
    return "\n\n".join(pieces)
