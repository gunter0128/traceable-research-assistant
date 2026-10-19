# 問答的流程：確認 workspace 是你的 → 問題轉向量 → 搜相關 chunk → 組 context 丟給 LLM → 整理成答案 + 引用來源
# 不寫 SQL 不碰 HTTP

import logging
import time
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core import log, openai_client
from app.repositories import chunk_repo
from app.schemas.qa import AskResponse, CitationOut
from app.services import usage_service, workspace_service

# 使用者最多等多久就要看到答案或錯誤訊息
# 原本 60 秒，改成 50 秒（docs/decisions/013）：CloudFront 等後端回應最多 60 秒，超過就直接回錯誤給使用者，
# 而 Lambda 冷啟動還要再加幾秒，留 10 秒的餘裕
ANSWER_BUDGET_SECONDS = 50

# 每份文件附上開頭的幾個字，讓模型知道段落出自哪份文件、哪個版本（docs/decisions/018）
DOCUMENT_HEADER_CHARS = 200


# 問答時 OpenAI 出問題：使用者正在等，不能延後，直接讓 API 回錯誤訊息
# 暫時性的（重試過還是不行）跟永久性的（設定有誤）分開，給使用者的訊息不同
class AIServiceUnavailable(Exception):
    pass


class AIServiceMisconfigured(Exception):
    pass


# today：給模型看的「今天日期」。線上不用給（用台灣當天）；評估時固定一個日期，分數才不會因為哪天跑而變
def ask(db: Session, workspace_id: int, owner_id: int, question: str, today: date | None = None) -> AskResponse:
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
            groups = _group_by_document(chunks)
            headers = chunk_repo.first_chunks(db, [document.id for document, _ in groups])
            fields["chunks"] = len(chunks)
            fields["documents"] = len(groups)
        context = _build_context(groups, headers, today or datetime.now(usage_service.TAIWAN).date())
        with log.timed("qa_generate") as fields:
            answer = openai_client.generate_answer(question, context, deadline=deadline, on_usage=record_usage)
            fields["answer_chars"] = len(answer or "")
        # 簡單版 citation：這次搜到、丟給 LLM 的 chunk 全部算數 不檢查答案有沒有每段都真的用到
        # 順序跟給模型看的編號 [1] [2]… 一樣（按文件分組），以後做逐句引用時才對得起來
        # 放在 try 裡面：要在下面 finally 寫入用量（commit）之前讀完段落的內容
        citations = [
            CitationOut(document_id=chunk.document_id, filename=document.filename, content=chunk.content)
            for document, group in groups
            for chunk in group
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


# 同一份文件的段落放在一起（docs/decisions/018）
# 文件的先後：照各自最相關的那一段排（搜尋結果本來就是由近到遠，dict 會記得第一次放進去的順序）
# 文件裡的段落：照原文的順序（chunk_index）排，讀起來跟原文一樣
def _group_by_document(chunks) -> list[tuple]:
    groups = {}
    for chunk in chunks:
        groups.setdefault(chunk.document_id, []).append(chunk)
    return [(group[0].document, sorted(group, key=lambda c: c.chunk_index)) for group in groups.values()]


# 把搜到的段落兜成給模型看的資料（docs/decisions/018）：
#   今天日期：模型不知道今天幾號，就判斷不了「10 月起調整」生效了沒有
#   每份文件附上檔名和開頭：一段文字切出來之後，常常看不出是哪份文件、哪個版本
#     開頭直接給模型讀，不自己解析日期：民國年、西元年、學年度各種寫法，模型都讀得懂
#   段落編號 [1] [2]… 跨文件連續編，跟 citations 的順序一樣
def _build_context(groups, headers: dict[int, str], today: date) -> str:
    blocks = [f"今天日期：{today.year} 年 {today.month} 月 {today.day} 日"]
    number = 0
    for i, (document, group) in enumerate(groups, start=1):
        # 把換行和連續空白壓成一個空格，開頭才不會佔太多行
        header = " ".join(headers.get(document.id, "").split())[:DOCUMENT_HEADER_CHARS]
        blocks.append(f"【文件 {i}】{document.filename}\n文件開頭：{header}")
        for chunk in group:
            number += 1
            blocks.append(f"[{number}] {chunk.content}")
    return "\n\n".join(blocks)
