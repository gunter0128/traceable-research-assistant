# 包 OpenAI SDK 的呼叫。純計算，不碰 DB / HTTP，跟 security.py 同一種角色
# 除了呼叫本身，也負責把 OpenAI 的各種錯誤分成「暫時性」和「永久性」兩類（見 docs/decisions/005）
# 分類完怎麼處理（重試、標失敗、回 503）由呼叫的 service 決定，這裡不管

import openai
from openai import OpenAI

from app.core.config import settings

EMBEDDING_MODEL = "text-embedding-3-small"
# 負責生成答案用的模型 跟上面 embedding 用的是不同任務 分開一個常數
GENERATION_MODEL = "gpt-4o-mini"

# 兩個 client，差別只在「最多等多久、重試幾次」，因為兩種情境能接受的等待時間不一樣
# timeout：單次呼叫最多等幾秒（SDK 預設 600 秒，太久）
# max_retries：SDK 內建的秒級重試次數，間隔大約 0.5、1、2、4 秒越來越長，也會遵守 OpenAI 回的「請等 N 秒」
# 文件處理（背景執行，沒人在等）：等久一點、多試幾次
_processing_client = OpenAI(api_key=settings.openai_api_key, timeout=30, max_retries=4)
# 問答（使用者在等）：問答要呼叫兩次（轉向量 + 生成），每次 15 秒 × 2 次，最壞加起來約 1 分鐘
_qa_client = OpenAI(api_key=settings.openai_api_key, timeout=15, max_retries=1)


# 等一下再試通常會好的錯誤：網路斷線、超時、速率限制、OpenAI 伺服器錯誤
class OpenAITemporaryError(Exception):
    pass


# 等多久都不會好、需要人處理的錯誤：key 無效、沒權限、餘額不足、請求格式錯誤
class OpenAIPermanentError(Exception):
    pass


# 把 OpenAI SDK 丟出來的錯誤，翻譯成上面兩種之一（SDK 自己的秒級重試已經用完了才會走到這裡）
def _classify(exc: openai.OpenAIError) -> Exception:
    # 連不上、超時（APITimeoutError 是 APIConnectionError 的子類別，一起接）
    if isinstance(exc, openai.APIConnectionError):
        return OpenAITemporaryError(str(exc))
    # 有收到 OpenAI 的回應，看狀態碼
    if isinstance(exc, openai.APIStatusError):
        # 陷阱：餘額不足跟速率限制都是 429，只能看錯誤代碼分辨。餘額不足等多久都不會好
        if exc.status_code == 429 and exc.code == "insufficient_quota":
            return OpenAIPermanentError(str(exc))
        # 408 請求逾時、409 衝突、429 速率限制、5xx 伺服器錯誤：跟 SDK 自己判斷「值得重試」的範圍一致
        if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
            return OpenAITemporaryError(str(exc))
    # 其他（400 格式錯、401 key 無效、403 沒權限、404 模型不存在……）
    return OpenAIPermanentError(str(exc))


# 何時呼叫：document_service 處理文件時，一次把一批 chunk 送去轉向量
# 一次送很多段，比一段一段送少很多次來回（300 頁約 330 段，每批 100 段只要呼叫 4 次）
def embed_texts(texts: list[str]) -> list[list[float]]:
    try:
        response = _processing_client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    # raise ... from exc：丟出新的錯誤，但保留原本的錯誤在 traceback 裡，方便除錯
    except openai.OpenAIError as exc:
        raise _classify(exc) from exc
    # response.data 是「每一段各自的結果」，每筆帶一個 index 對應它是第幾段輸入
    # 照 index 排好再取向量，保證第 i 個向量一定對應第 i 段文字
    return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


# 何時呼叫：qa_service 把使用者的問題轉成向量，拿去跟 chunk 比相似度
def embed_query(text: str) -> list[float]:
    try:
        response = _qa_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    except openai.OpenAIError as exc:
        raise _classify(exc) from exc
    return response.data[0].embedding


# 何時呼叫：qa_service 把問題跟搜到的 chunk 內容組成 context 之後，丟給 LLM 生成答案
# system 那句話是強制「只能照資料回答，答不出來要老實說」防止 LLM 亂編答案
def generate_answer(question: str, context: str) -> str:
    try:
        response = _qa_client.chat.completions.create( # 對話生成的功能
            model=GENERATION_MODEL,
            messages=[
                {
                    "role": "system", # 開發者給 LLM 的規則
                    "content": (
                        "你是一個只根據提供的資料回答問題的助手。"
                        "只能使用下面「資料」裡的內容回答，不能使用你自己的知識。"
                        "如果資料裡沒有足夠的資訊回答這個問題，"
                        "就明確說「文件中找不到相關資訊」，不要編造答案。"
                    ),
                },
                {"role": "user", "content": f"資料：\n{context}\n\n問題：{question}"}, # 真正的使用者輸入
            ],
        )
    except openai.OpenAIError as exc:
        raise _classify(exc) from exc
    return response.choices[0].message.content # 選擇第一個候選回應(一個問題可能有好幾種回應)
