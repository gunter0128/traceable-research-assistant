# 用量相關的 API 回應形狀

from decimal import Decimal

from pydantic import BaseModel


class UsageTodayOut(BaseModel):
    # GET /usage/today 回這個：今天（台灣時間）用了多少、上限多少、到了沒
    used_usd: Decimal
    limit_usd: Decimal
    limit_reached: bool
