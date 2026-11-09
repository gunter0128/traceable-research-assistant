# 包 OpenAI SDK 的呼叫（轉向量、生成答案），不碰資料庫，跟 s3_client.py 同一種角色
# 送什麼內容（回答原則、資料、問題）由呼叫的 service 決定，這裡只負責送出去，另外負責兩件事：
#   1. 把 OpenAI 的各種錯誤分成「暫時性」和「永久性」兩類（見 docs/decisions/005）
#   2. 自己做重試（見 docs/decisions/008）：SDK 內建的重試會照 OpenAI 的「請等 N 秒」最多等 120 秒，
#      而且在 embeddings.create() 裡面自己做，我們插不上手，沒辦法在重試之間報平安、也沒辦法控制總時間
# 分類完怎麼處理（標失敗、回 503）由呼叫的 service 決定，這裡不管

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

import openai
from openai import OpenAI

from app.core import log
from app.core.config import settings

# 用哪個模型轉向量、回答，看設定（docs/decisions/037）：公開展示版用 OpenAI，企業內部版用本機的模型
EMBEDDING_MODEL = settings.embedding_model
# 負責生成答案用的模型 跟上面 embedding 用的是不同任務 分開一個常數
# 2026-10 從 gpt-4o-mini 換成 gpt-4o：「適不適用於問的人」的題目答得好很多，每題成本約 16 倍（docs/decisions/023）
GENERATION_MODEL = settings.answer_model

# 每 100 萬個 token 多少美元：(輸入, 輸出)。embedding 只有輸入
# 2026-10 OpenAI 官網的價格，OpenAI 調價時要跟著改（docs/decisions/012）
PRICES_PER_MILLION = {
    "text-embedding-3-small": (Decimal("0.02"), Decimal(0)),
    "gpt-4o": (Decimal("2.50"), Decimal(10)),
}
# 自己架的模型不用付 API 的錢（GPU、電費另外算），記 0
# 只有接自己的服務時才補：用 OpenAI 時模型名字打錯，算錢那一步就會報錯，不會默默記成 0
if settings.embedding_base_url:
    PRICES_PER_MILLION[EMBEDDING_MODEL] = (Decimal(0), Decimal(0))
if settings.answer_base_url:
    PRICES_PER_MILLION[GENERATION_MODEL] = (Decimal(0), Decimal(0))


# 一次呼叫用了多少（OpenAI 每次回應都會附上）
# @dataclass：自動產生 __init__ 等方法的類別，適合用來裝「幾個欄位綁在一起」的資料
@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int = 0

    # 這次花了多少美元
    def cost_usd(self) -> Decimal:
        input_price, output_price = PRICES_PER_MILLION[self.model]
        return (self.input_tokens * input_price + self.output_tokens * output_price) / 1_000_000

# max_retries=0：關掉 SDK 內建的重試，全部由下面的 _with_retries 處理
# 每次嘗試最多等多久，呼叫時才用 with_options(timeout=...) 指定（文件處理跟問答不一樣）
# 企業內部版沒有 OpenAI 的金鑰：SDK 沒給金鑰會直接報錯，先給一個假的，真的去呼叫 OpenAI 時才會失敗（金鑰錯誤，歸類成永久性錯誤）
client = OpenAI(api_key=settings.openai_api_key or "not-set", max_retries=0)


# 自己架的服務（vLLM）不檢查金鑰，但 SDK 一定要給一個
def _client_for(base_url: str | None) -> OpenAI:
    return client if base_url is None else OpenAI(base_url=base_url, api_key="not-needed", max_retries=0)


# 轉向量用的服務：沒設定就是 OpenAI
embedding_client = _client_for(settings.embedding_base_url)

# 文件處理（背景執行，沒人在等）：每次最多等 30 秒、最多試 5 次；OpenAI 叫我們等，最多等 60 秒
PROCESSING_TIMEOUT = 30
PROCESSING_ATTEMPTS = 5
PROCESSING_MAX_WAIT = 60
# 問答（使用者在等）：每次最多等 15 秒、最多試 2 次；OpenAI 叫我們等超過 5 秒就直接放棄
# 總時間由 qa_service 給的 deadline 控制（整個問答共用 50 秒）
QA_TIMEOUT = 15
# 生成答案每次最多等幾秒：OpenAI 用 15 秒；本機的模型比較慢，看設定（docs/decisions/037）
ANSWER_TIMEOUT = settings.answer_timeout
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
# on_usage：成功之後呼叫，把這次用了多少交給呼叫的人記錄（docs/decisions/012）
#   只有成功才會呼叫：失敗的請求 OpenAI 不收錢
def embed_texts(
    texts: list[str],
    on_attempt: Callable[[], None] | None = None,
    on_usage: Callable[[Usage], None] | None = None,
) -> list[list[float]]:
    def call(timeout: float):
        # with_options(timeout=...)：只有這一次呼叫用這個等待上限，不影響其他地方
        return embedding_client.with_options(timeout=timeout).embeddings.create(
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
    _report_embedding_usage(response, on_usage)
    # response.data 是「每一段各自的結果」，每筆帶一個 index 對應它是第幾段輸入
    # 照 index 排好再取向量，保證第 i 個向量一定對應第 i 段文字
    return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


# 何時呼叫：qa_service 把使用者的問題轉成向量，拿去跟 chunk 比相似度
# deadline：整個問答共用的截止時間（qa_service 算好傳進來）
# 問題前面加上模型要求的說明（設定的 embedding_query_prefix，OpenAI 不用加）；文件段落不加，見 embed_texts
def embed_query(
    text: str,
    deadline: float | None = None,
    on_usage: Callable[[Usage], None] | None = None,
) -> list[float]:
    def call(timeout: float):
        return embedding_client.with_options(timeout=timeout).embeddings.create(
            model=EMBEDDING_MODEL, input=[settings.embedding_query_prefix + text]
        )

    response = _with_retries(
        call,
        attempts=QA_ATTEMPTS,
        timeout=QA_TIMEOUT,
        max_wait=QA_MAX_WAIT,
        give_up_on_long_wait=True,
        deadline=deadline,
    )
    _report_embedding_usage(response, on_usage)
    return response.data[0].embedding


# 回答用的模型服務（docs/decisions/034、037）：看設定，沒設定就是 OpenAI
# 也可以換成任何「OpenAI 格式」的服務，例如在內部 GPU 上用 vLLM 架的開源模型：機密文件不用送到外部
# answer_extra_body：那個服務需要的額外參數（例如 Qwen 建議的抽字設定、關掉思考模式），OpenAI 不需要
answer_client = _client_for(settings.answer_base_url)
answer_extra_body: dict | None = settings.answer_extra_body


# 評估時換別的回答服務比較用（eval/run.py --answer-base-url），線上看設定
def use_answer_endpoint(base_url: str, extra_body: dict | None = None) -> None:
    global answer_client, answer_extra_body
    answer_client = _client_for(base_url)
    answer_extra_body = extra_body


# 何時呼叫：qa_service 組好要給模型看的內容之後，丟給 LLM 生成答案
# system：回答原則（開發者給模型的規則）；user：資料 + 問題。兩段的內容都由 qa_service 決定，這裡只負責送出去
def generate_answer(
    system: str,
    user: str,
    deadline: float | None = None,
    on_usage: Callable[[Usage], None] | None = None,
) -> str:
    def call(timeout: float):
        return answer_client.with_options(timeout=timeout).chat.completions.create( # 對話生成的功能
            model=GENERATION_MODEL,
            extra_body=answer_extra_body,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )

    response = _with_retries(
        call,
        attempts=QA_ATTEMPTS,
        timeout=ANSWER_TIMEOUT,
        max_wait=QA_MAX_WAIT,
        give_up_on_long_wait=True,
        deadline=deadline,
    )
    if on_usage is not None:
        # prompt_tokens：送進去的（問題 + 資料 + 規則）；completion_tokens：生成出來的答案
        on_usage(Usage(GENERATION_MODEL, response.usage.prompt_tokens, response.usage.completion_tokens))
    # 選擇第一個候選回應(一個問題可能有好幾種回應)
    # 開源模型開思考模式時，想到長度上限還沒想完，就沒有答案（content 是 None），當成空的回答
    return response.choices[0].message.content or ""


# embedding 的回應只有輸入的 token 數（沒有生成任何文字）
def _report_embedding_usage(response, on_usage: Callable[[Usage], None] | None) -> None:
    if on_usage is not None:
        on_usage(Usage(EMBEDDING_MODEL, response.usage.prompt_tokens))
