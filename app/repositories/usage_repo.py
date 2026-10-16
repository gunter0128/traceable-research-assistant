# usage_records 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.usage_record import UsageRecord


# 何時呼叫：每次 OpenAI 回應成功、知道這次用了多少 token 之後
def add(
    db: Session,
    *,
    user_id: int,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    document_id: int | None = None,
) -> None:
    db.add(
        UsageRecord(
            user_id=user_id,
            purpose=purpose,
            document_id=document_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
    )
    db.commit()


# since 之後總共花了多少錢。user_id = None 代表全系統加總
def total_cost_since(db: Session, since: datetime, user_id: int | None = None) -> Decimal:
    # func.sum：SQL 的 SUM，在資料庫裡加總，不用把每一筆都拿回來再用 Python 加
    # func.coalesce(..., 0)：一筆都沒有時 SUM 會是 NULL，換成 0
    query = db.query(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).filter(
        UsageRecord.created_at >= since
    )
    if user_id is not None:
        query = query.filter(UsageRecord.user_id == user_id)
    # .scalar()：查詢結果只有一個值，直接拿出來
    return Decimal(query.scalar())
