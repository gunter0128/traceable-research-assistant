# usage_records 表 每呼叫一次 OpenAI 一列：誰用的、做什麼用、哪個模型、多少 token、多少錢（docs/decisions/012）
# 用途：每次問答 / 上傳前查「今天已經用了多少」，超過上限就擋；也能看錢花在哪

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, func

from app.core.db import Base


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True)
    # ondelete="CASCADE"：使用者被刪掉時，他的用量紀錄由資料庫一起刪掉
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # qa = 問答、document = 處理文件
    purpose = Column(String, nullable=False)
    # 處理哪份文件花的（問答是空的）。不設外鍵：文件刪掉之後，花掉的錢還是要算在當天的用量裡
    document_id = Column(Integer, nullable=True)
    model = Column(String, nullable=False)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False, server_default="0")
    # Numeric(12, 8)：精確的小數，總共 12 位數、小數點後 8 位
    # 錢不用 float：float 是近似值，0.1 + 0.2 會變成 0.30000000000000004，加總很多筆之後誤差會累積
    # 一次問題轉向量只要 0.0000004 美元，所以小數點後要留到 8 位
    cost_usd = Column(Numeric(12, 8), nullable=False)
    # index=True：查「今天用了多少」是用時間篩選，加索引查起來比較快
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
