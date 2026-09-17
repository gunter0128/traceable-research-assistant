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
    status = Column(String, nullable=False, server_default="uploaded")
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    workspace = relationship("Workspace", back_populates="documents")
    chunks = relationship(
        "Chunk", back_populates="document", cascade="all, delete-orphan" # 刪掉一個 Document 時 它底下的 Chunk 會一起被刪
    )
