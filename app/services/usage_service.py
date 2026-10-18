# 用量的規則：記下每次呼叫 OpenAI 花了多少錢、檢查今天有沒有超過上限（docs/decisions/012）
# 不寫 SQL 不碰 HTTP

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core import log
from app.core.openai_client import Usage
from app.repositories import usage_repo

# 每人每天最多花多少美元。正常使用一天花不到 0.05，0.5 大約可以問 800 個問題
USER_DAILY_LIMIT_USD = Decimal("0.5")
# 全系統每天最多花多少美元：任何人都能註冊，只限制每人的話，註冊很多帳號就繞過了
# 這是最後一道防線，保證一天的帳單最多就是這麼多
SYSTEM_DAILY_LIMIT_USD = Decimal(5)
# 「今天」用台灣時間算：使用者在台灣，「明天再試」指的是台灣的半夜 12 點之後
# 台灣沒有日光節約時間，固定 +8 小時就好
TAIWAN = timezone(timedelta(hours=8))


# 今天的用量已經到上限了。scope：user = 這個人自己的上限、system = 全系統的上限
class QuotaExceeded(Exception):
    # 錯誤物件上多帶一個 scope，API 層看它決定回哪一句訊息
    # super().__init__(scope)：先照一般 Exception 的方式建好（錯誤訊息就是 scope）
    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


@dataclass
class UsageToday:
    used_usd: Decimal
    limit_usd: Decimal
    # 自己或全系統任一個到上限，就算到了
    limit_reached: bool


# 今天（台灣時間）半夜 12 點
def _start_of_today() -> datetime:
    return datetime.now(TAIWAN).replace(hour=0, minute=0, second=0, microsecond=0)


# 何時呼叫：問答、上傳、重試「開始之前」
# 判斷的是「開始之前」的用量：只要還沒到上限就放行，這一次會完整做完，就算做完之後超過了也一樣
# 實際用量最多會超過上限「一次動作」的量（最貴是剛好卡在邊緣時處理一份 300 頁的文件，約 0.004 美元）
def ensure_within_quota(db: Session, user_id: int) -> None:
    since = _start_of_today()
    # 先檢查全系統：全系統到了的話，每個人都不能用，訊息也不一樣
    if usage_repo.total_cost_since(db, since) >= SYSTEM_DAILY_LIMIT_USD:
        log.event("quota_exceeded", logging.WARNING, scope="system", user_id=user_id)
        raise QuotaExceeded("system")
    if usage_repo.total_cost_since(db, since, user_id) >= USER_DAILY_LIMIT_USD:
        log.event("quota_exceeded", logging.WARNING, scope="user", user_id=user_id)
        raise QuotaExceeded("user")


# 何時呼叫：前端在問答完、文件處理完之後來查，到上限了就馬上顯示，不用等下一次被擋才知道
def today(db: Session, user_id: int) -> UsageToday:
    since = _start_of_today()
    used = usage_repo.total_cost_since(db, since, user_id)
    system_used = usage_repo.total_cost_since(db, since)
    return UsageToday(
        used_usd=used,
        limit_usd=USER_DAILY_LIMIT_USD,
        limit_reached=used >= USER_DAILY_LIMIT_USD or system_used >= SYSTEM_DAILY_LIMIT_USD,
    )


# 何時呼叫：OpenAI 回應成功之後（openai_client 的 on_usage）
def record(
    db: Session, user_id: int, purpose: str, usage: Usage, document_id: int | None = None
) -> None:
    cost = usage.cost_usd()
    usage_repo.add(
        db,
        user_id=user_id,
        purpose=purpose,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        document_id=document_id,
    )
    # 也寫進 log，在 CloudWatch 就能統計，不用連資料庫
    # float(cost)：JSON 沒有 Decimal 型別，log 裡用一般數字就夠了（精確的值在資料庫）
    log.event(
        "openai_usage", purpose=purpose, model=usage.model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, cost_usd=float(cost),
    )
