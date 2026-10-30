# 問答的流程：確認 workspace 是你的 → 問題轉向量 → 搜相關 chunk → 組 context 丟給 LLM → 整理成答案 + 引用來源
# 給模型的回答原則（prompt）也在這裡：要送什麼給模型是問答的規則，openai_client 只負責送出去
# 不寫 SQL 不碰 HTTP

import logging
import re
import time
from dataclasses import dataclass
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

# 每次問答搜幾段給模型看（docs/decisions/020）
SEARCH_LIMIT = 5

# 給模型的回答原則（system prompt，docs/decisions/019）
# 原本只有「只能照資料回答、答不出來就說找不到」，沒教模型怎麼讀：一碰到「不適用」「分情況」就退回「找不到」
# 每一條都要是通用的閱讀原則：套到租屋合約、學校規章、論文上也成立才能放
# 刻意不放任何領域的詞（年資、職級、學年度…）和例子，避免只對某一種文件有效
ANSWER_RULES = """你是一個根據使用者提供的文件回答問題的助手。

回答原則：
1. 只能根據「資料」回答，不能用你自己的知識補充，也不能推測文件沒寫的內容。
2. 先給結論。問題沒有條件、資料也沒有分情況時，直接回答，不要加「視情況而定」之類的保留。
3. 答案會因為對象、情況或時間而不同時：問題已經說明是誰、什麼情況，就給適用的那一個；沒說明的話，列出每種情況各自的答案。
4. 資料寫明某條規定「不適用」某些人或情況時，這本身就是答案：直接說不適用和依據，資料有寫替代的做法也一起說。不要因此說找不到。
5. 多份資料說法不同時，看各自的日期、版本、有沒有被修訂或廢止，用今天的日期判斷哪一個現在有效，並說明舊的為什麼不適用。修訂只改一部分時，沒改到的部分照原本的。只找到可能已經過時的版本時，要明白說出來。
6. 用到資料的句子，句尾標上依據的段落編號，例如「……[2]。」，用到好幾段就寫「[1][3]」。結論後面再寫一行依據：文件名稱、條次；有生效日期就一起寫。
7. 資料完全沒有相關資訊時，才說「文件中找不到相關資訊」；只有一部分資訊時，回答有的部分，並說明缺什麼。"""

# 資料裡找不到答案時，模型會說的話（跟上面第 7 條的說法一樣）
NOT_FOUND = "文件中找不到相關資訊"

# 放在問題後面的提醒：只寫在 system 的原則裡，約三成的回答會忘記標（docs/decisions/021）
# 模型對離問題最近的指示比較會照做
CITATION_REMINDER = "（回答時，用到資料的句子，句尾要標上依據的段落編號，例如 [1]。）"


# 問答時 OpenAI 出問題：使用者正在等，不能延後，直接讓 API 回錯誤訊息
# 暫時性的（重試過還是不行）跟永久性的（設定有誤）分開，給使用者的訊息不同
class AIServiceUnavailable(Exception):
    pass


class AIServiceMisconfigured(Exception):
    pass


# today：給模型看的「今天日期」。線上不用給（用台灣當天）；評估時固定一個日期，分數才不會因為哪天跑而變
def ask(db: Session, workspace_id: int, owner_id: int, question: str, today: date | None = None) -> AskResponse:
    response, _ = ask_with_passages(db, workspace_id, owner_id, question, today)
    return response


# 跟 ask 一樣，另外回傳「這次給模型看的全部段落」
# API 只需要答案引用到的；評估要分開看「搜到了哪些」（檢索）跟「答案引用了哪些」（引用）
def ask_with_passages(
    db: Session, workspace_id: int, owner_id: int, question: str, today: date | None = None
) -> tuple[AskResponse, list[CitationOut]]:
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
    try:
        groups, headers = retrieve(db, workspace_id, question, deadline=deadline, on_usage=record_usage)
        today = today or datetime.now(usage_service.TAIWAN).date()
        context = build_context(groups, headers, today)
        with log.timed("qa_generate") as fields:
            answer = openai_client.generate_answer(
                ANSWER_RULES, user_message(context, question), deadline=deadline, on_usage=record_usage
            )
            fields["answer_chars"] = len(answer)
        # 給模型看的全部段落，number 就是給模型看的編號 [1] [2]…（順序跟 build_context 一樣）
        # 放在 try 裡面：要在下面 finally 寫入用量（commit）之前讀完段落的內容
        passages = [
            CitationOut(number=number, document_id=chunk.document_id, filename=document.filename, content=chunk.content)
            for number, (document, chunk) in enumerate(
                ((document, chunk) for document, group in groups for chunk in group), start=1
            )
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

    # 逐句引用（docs/decisions/021）：只回傳答案裡真的標到的段落
    answer, cited = _extract_citations(answer, len(passages))
    if cited:
        citations = [passage for passage in passages if passage.number in cited]
    elif NOT_FOUND in answer:
        # 說找不到的答案不附引用：不然會變成「答案說找不到，下面卻附了一份文件」
        citations = []
    else:
        # 安全網：模型有回答、卻忘了標編號（評估時約三成），退回列出搜到的全部段落，至少看得到來源
        # 前端看答案裡沒有 [n]，會註明「沒有逐句標註，以下是搜尋到的段落」
        citations = passages
    return AskResponse(answer=answer, citations=citations), passages


# 搜段落：問題轉向量 → 搜最像的幾段 → 照文件分組、相連的合併 → 查每份文件的開頭
# 回傳 (分好組的段落, {document_id: 文件開頭})。訓練資料也用這個（train/make_answers.py），給模型看的資料跟線上一樣
# 每一段各記花多久，回答「問答慢在哪」（docs/decisions/010）；問題只記字數、不記內容：使用者的問題可能是未發表的研究
def retrieve(
    db: Session, workspace_id: int, question: str, deadline: float | None = None, on_usage=None
) -> tuple[list[tuple], dict[int, str]]:
    with log.timed("qa_embed", question_chars=len(question)):
        embedding = openai_client.embed_query(question, deadline=deadline, on_usage=on_usage)
    with log.timed("qa_search") as fields:
        chunks = chunk_repo.search_similar(db, workspace_id, embedding, limit=SEARCH_LIMIT)
        groups = _merge_adjacent(_group_by_document(chunks))
        headers = chunk_repo.first_chunks(db, [document.id for document, _ in groups])
        fields["chunks"] = len(chunks)
        fields["documents"] = len(groups)
    return groups, headers


# 給模型的 user 訊息：資料 + 問題，最後再提醒一次標引用
def user_message(context: str, question: str) -> str:
    return f"資料：\n{context}\n\n問題：{question}\n\n{CITATION_REMINDER}"


def _date_text(day: date) -> str:
    return f"{day.year} 年 {day.month} 月 {day.day} 日"


# 答案裡的引用標記：[1]、[1][2]、[1, 2]、全形的［1］都算
_MARKER = re.compile(r"[\[［]\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[\]］]")


# 從答案裡找出引用了哪幾段，並把標記統一寫成 [1][2]
# 同一個標記裡有幾個編號超出範圍的（例如只給 5 段，卻寫 [1, 9]），只留範圍內的 [1]
# 整個標記都不在範圍內的，原文不動：那可能根本不是引用（例如「[2025]」）。
#   也可能是模型編出來的編號，但這很少見，寧可留著，也不要誤刪原文
def _extract_citations(answer: str, count: int) -> tuple[str, set[int]]:
    cited = set()

    def normalize(match: re.Match) -> str:
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        valid = [n for n in dict.fromkeys(numbers) if 1 <= n <= count]
        if not valid:
            return match.group()
        cited.update(valid)
        return "".join(f"[{n}]" for n in valid)

    return _MARKER.sub(normalize, answer), cited


# 同一份文件的段落放在一起（docs/decisions/018）
# 文件的先後：照各自最相關的那一段排（搜尋結果本來就是由近到遠，dict 會記得第一次放進去的順序）
# 文件裡的段落：照原文的順序（chunk_index）排，讀起來跟原文一樣
def _group_by_document(chunks) -> list[tuple]:
    groups = {}
    for chunk in chunks:
        groups.setdefault(chunk.document_id, []).append(chunk)
    return [(group[0].document, sorted(group, key=lambda c: c.chunk_index)) for group in groups.values()]


# 給模型看的一段：可能是好幾個相連的 chunk 合併成的（_merge_adjacent）
# 欄位名稱跟 chunk 一樣（document_id、content），後面組 context、組引用的程式不用分是哪一種
# last_index：最後一個 chunk 的編號，下一個 chunk 接不接得上看它
@dataclass
class Passage:
    document_id: int
    last_index: int
    content: str


# 同一份文件前後相連的段落（chunk_index 連號）合併成一段、只給一個編號（docs/decisions/043）
# 一條規定常被切成相連的兩段，兩段都搜到時，模型知道答案出自哪一條，卻常標到另一半：標錯的句子約六成是這種
# 產生新的 Passage，不改搜出來的 chunk：那是資料庫的物件，改了內容可能被寫回資料庫
def _merge_adjacent(groups) -> list[tuple]:
    merged = []
    for document, group in groups:
        passages = []
        for chunk in group:
            if passages and chunk.chunk_index == passages[-1].last_index + 1:
                passages[-1].content = _join_overlap(passages[-1].content, chunk.content)
                passages[-1].last_index = chunk.chunk_index
            else:
                passages.append(Passage(chunk.document_id, chunk.chunk_index, chunk.content))
        merged.append((document, passages))
    return merged


# 接起相連的兩段：切段時相鄰兩段重疊約 50 個 token（chunker.chunk_text）
# 找「前一段的結尾 = 後一段的開頭」最長的那一截，只留一份；找不到就直接換行接上
# 至少 10 個字才算重疊，太短的可能只是剛好一樣
def _join_overlap(first: str, second: str) -> str:
    for size in range(min(len(first), len(second)), 9, -1):
        if first.endswith(second[:size]):
            return first + second[size:]
    return first + "\n" + second


# 把搜到的段落兜成給模型看的資料（docs/decisions/018）：
#   今天日期：模型不知道今天幾號，就判斷不了「10 月起調整」生效了沒有
#   每份文件附上檔名和開頭：一段文字切出來之後，常常看不出是哪份文件、哪個版本
#     開頭直接給模型讀，不自己解析日期：民國年、西元年、學年度各種寫法，模型都讀得懂
#   文件不編號、只標檔名：編了號，模型會寫「依據：文件 1」，使用者看不到文件 1 是哪一份
#   段落編號 [1] [2]… 跨文件連續編，跟 citations 的順序一樣
def build_context(groups, headers: dict[int, str], today: date) -> str:
    blocks = [f"今天日期：{_date_text(today)}"]
    number = 0
    for document, group in groups:
        # 把換行和連續空白壓成一個空格，開頭才不會佔太多行
        header = " ".join(headers.get(document.id, "").split())[:DOCUMENT_HEADER_CHARS]
        blocks.append(f"【文件】{document.filename}\n文件開頭：{header}")
        for chunk in group:
            number += 1
            blocks.append(f"[{number}] {chunk.content}")
    return "\n\n".join(blocks)
