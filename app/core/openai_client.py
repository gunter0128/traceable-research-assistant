# 包 OpenAI SDK 的呼叫。純計算，不碰 DB / HTTP，跟 security.py 同一種角色
# 負責兩件事：
#   1. 把 OpenAI 的各種錯誤分成「暫時性」和「永久性」兩類（見 docs/decisions/005）
#   2. 自己做重試（見 docs/decisions/008）：SDK 內建的重試會照 OpenAI 的「請等 N 秒」最多等 120 秒，
#      而且在 embeddings.create() 裡面自己做，我們插不上手，沒辦法在重試之間報平安、也沒辦法控制總時間
# 分類完怎麼處理（延後、標失敗、回 503）由呼叫的 service 決定，這裡不管

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import openai
from openai import OpenAI

from app.core import log
from app.core.config import settings

EMBEDDING_MODEL = "text-embedding-3-small"
# 負責生成答案用的模型 跟上面 embedding 用的是不同任務 分開一個常數
GENERATION_MODEL = "gpt-4o-mini"

# max_retries=0：關掉 SDK 內建的重試，全部由下面的 _with_retries 處理
# 每次嘗試最多等多久，呼叫時才用 with_options(timeout=...) 指定（文件處理跟問答不一樣）
client = OpenAI(api_key=settings.openai_api_key, max_retries=0)

# 文件處理（背景執行，沒人在等）：每次最多等 30 秒、最多試 5 次；OpenAI 叫我們等，最多等 60 秒
PROCESSING_TIMEOUT = 30
PROCESSING_ATTEMPTS = 5
PROCESSING_MAX_WAIT = 60
# 問答（使用者在等）：每次最多等 15 秒、最多試 2 次；OpenAI 叫我們等超過 5 秒就直接放棄
# 總時間由 qa_service 給的 deadline 控制（整個問答共用 60 秒）
QA_TIMEOUT = 15
QA_ATTEMPTS = 2
QA_MAX_WAIT = 5

# 重試間隔：0.5、1、2、4 秒每次加倍，最多 8 秒（跟 SDK 原本的規則一樣）
_INITIAL_DELAY = 0.5
_MAX_DELAY = 8.0


# 等一下再試通常會好的錯誤：網路斷線、超時、速率限制、OpenAI 伺服器錯誤
class OpenAITemporaryError(Exception):
    pass


# 等多久都不會好、需要人處理的錯誤：key 無效、沒權限、餘額不足、請求格式錯誤
class OpenAIPermanentError(Exception):
    pass


# 把 OpenAI SDK 丟出來的錯誤，翻譯成上面兩種之一
def _classify(exc: openai.OpenAIError) -> Exception:
    # 連不上、超時（APITimeoutError 是 APIConnectionError 的子類別，一起接）
    if isinstance(exc, openai.APIConnectionError):
        return OpenAITemporaryError(str(exc))
    # 有收到 OpenAI 的回應，看狀態碼
    if isinstance(exc, openai.APIStatusError):
        # 陷阱：餘額不足跟速率限制都是 429，只能看錯誤代碼分辨。餘額不足等多久都不會好
        if exc.status_code == 429 and exc.code == "insufficient_quota":
            return OpenAIPermanentError(str(exc))
        # 408 請求逾時、409 衝突、429 速率限制、5xx 伺服器錯誤
        if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
            return OpenAITemporaryError(str(exc))
    # 其他（400 格式錯、401 key 無效、403 沒權限、404 模型不存在……）
    return OpenAIPermanentError(str(exc))


# OpenAI 有沒有在回應裡說「請等 N 秒」？有的話回傳秒數，沒有回傳 None
# retry-after-ms 是毫秒、retry-after 是秒；retry-after 也可能是日期格式，那種就不理它
def _retry_after_seconds(exc: openai.OpenAIError) -> float | None:
    if not isinstance(exc, openai.APIStatusError):
        return None
    headers = exc.response.headers
    for name, unit in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(name)
        if value is not None:
            try:
                return float(value) / unit
            except ValueError:
                pass
    return None


# 沒有「請等 N 秒」時用的間隔：0.5 × 2^(第幾次重試)，最多 8 秒
# 再乘上 0.75～1 之間的隨機數（jitter）：很多程式同時失敗時，不會同一瞬間一起重試，把 OpenAI 又打爆一次
def _backoff_seconds(retry_number: int) -> float:
    delay = min(_INITIAL_DELAY * 2**retry_number, _MAX_DELAY)
    return delay * (1 - 0.25 * random.random())


T = TypeVar("T")


# 重試的迴圈。call(timeout) 是真正去呼叫 OpenAI 的函式，傳入這次最多等幾秒
# attempts：最多試幾次；timeout：每次最多等幾秒；max_wait：OpenAI 叫我們等的上限
# give_up_on_long_wait：OpenAI 叫我們等超過上限時，True = 直接放棄（問答），False = 等上限那麼久就好（文件處理）
# deadline：time.monotonic() 的截止時間，超過就放棄（問答用來控制總時間）；None = 不限
# on_attempt：每次嘗試前呼叫（文件處理用來報平安）
def _with_retries(
    call: Callable[[float], T],
    *,
    attempts: int,
    timeout: float,
    max_wait: float,
    give_up_on_long_wait: bool,
    deadline: float | None = None,
    on_attempt: Callable[[], None] | None = None,
) -> T:
    for attempt in range(attempts):
        this_timeout = timeout
        if deadline is not None:
            # time.monotonic()：只會往前走的時鐘，適合算「過了多久」（不受電腦改時間影響）
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise OpenAITemporaryError("時間預算用完了")
            this_timeout = min(timeout, remaining)
        if on_attempt is not None:
            on_attempt()
        try:
            return call(this_timeout)
        except openai.OpenAIError as exc:
            error = _classify(exc)
            # 永久性錯誤：試幾次都一樣，直接丟出去
            if isinstance(error, OpenAIPermanentError) or attempt == attempts - 1:
                raise error from exc
            retry_after = wait = _retry_after_seconds(exc)
            if wait is None:
                wait = _backoff_seconds(attempt)
            elif wait > max_wait:
                if give_up_on_long_wait:
                    raise error from exc
                wait = max_wait
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise error from exc
            # 每次重試記一筆：事後才看得出 OpenAI 多常出問題、出的是哪種問題、OpenAI 有沒有叫我們等
            log.event(
                "openai_retry", logging.WARNING,
                attempt=attempt + 1, error=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
                retry_after=retry_after, wait_seconds=round(wait, 2),
            )
            time.sleep(wait)
    # 迴圈一定會在裡面 return 或 raise，走不到這裡；寫出來讓型別檢查知道
    raise OpenAITemporaryError("重試次數用完了")


# 何時呼叫：document_service 處理文件時，一次把一批 chunk 送去轉向量
# 一次送很多段，比一段一段送少很多次來回（300 頁約 330 段，每批 100 段只要呼叫 4 次）
# on_attempt：每次嘗試前呼叫，document_service 用它報平安（docs/decisions/008）
def embed_texts(
    texts: list[str], on_attempt: Callable[[], None] | None = None
) -> list[list[float]]:
    def call(timeout: float):
        # with_options(timeout=...)：只有這一次呼叫用這個等待上限，不影響其他地方
        return client.with_options(timeout=timeout).embeddings.create(
            model=EMBEDDING_MODEL, input=texts
        )

    response = _with_retries(
        call,
        attempts=PROCESSING_ATTEMPTS,
        timeout=PROCESSING_TIMEOUT,
        max_wait=PROCESSING_MAX_WAIT,
        give_up_on_long_wait=False,
        on_attempt=on_attempt,
    )
    # response.data 是「每一段各自的結果」，每筆帶一個 index 對應它是第幾段輸入
    # 照 index 排好再取向量，保證第 i 個向量一定對應第 i 段文字
    return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


# 何時呼叫：qa_service 把使用者的問題轉成向量，拿去跟 chunk 比相似度
# deadline：整個問答共用的截止時間（qa_service 算好傳進來）
def embed_query(text: str, deadline: float | None = None) -> list[float]:
    def call(timeout: float):
        return client.with_options(timeout=timeout).embeddings.create(
            model=EMBEDDING_MODEL, input=text
        )

    response = _with_retries(
        call,
        attempts=QA_ATTEMPTS,
        timeout=QA_TIMEOUT,
        max_wait=QA_MAX_WAIT,
        give_up_on_long_wait=True,
        deadline=deadline,
    )
    return response.data[0].embedding


# 何時呼叫：qa_service 把問題跟搜到的 chunk 內容組成 context 之後，丟給 LLM 生成答案
# system 那句話是強制「只能照資料回答，答不出來要老實說」防止 LLM 亂編答案
def generate_answer(question: str, context: str, deadline: float | None = None) -> str:
    def call(timeout: float):
        return client.with_options(timeout=timeout).chat.completions.create( # 對話生成的功能
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

    response = _with_retries(
        call,
        attempts=QA_ATTEMPTS,
        timeout=QA_TIMEOUT,
        max_wait=QA_MAX_WAIT,
        give_up_on_long_wait=True,
        deadline=deadline,
    )
    return response.choices[0].message.content # 選擇第一個候選回應(一個問題可能有好幾種回應)
