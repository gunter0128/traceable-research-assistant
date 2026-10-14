# documents 表 一份文件一列 屬於一個 workspace

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import relationship

from app.core.db import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id"), nullable=False, index=True
    )
    filename = Column(String, nullable=False)
    storage_path = Column(String, nullable=False)
    # uploaded = 還沒處理完（等工單 / 處理中）、processed = 完成、failed = 失敗
    status = Column(String, nullable=False, server_default="uploaded")
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    # 被處理過幾次。超過上限就不再重試，避免一份每次都會讓程式當掉的文件被無限重送
    attempts = Column(Integer, nullable=False, server_default="0")
    # 最近一次送工單的時間。定期掃描靠它判斷「送出去很久了還沒處理完 = 卡住了」
    queued_at = Column(DateTime(timezone=True), nullable=True)
    # 失敗原因，給使用者看的（例如「抓不到文字，可能是掃描檔」）。沒失敗時是空的
    error_message = Column(String, nullable=True)

    workspace = relationship("Workspace", back_populates="documents")
    chunks = relationship(
        "Chunk", back_populates="document", cascade="all, delete-orphan" # 刪掉一個 Document 時 它底下的 Chunk 會一起被刪
    )
