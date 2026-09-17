# chunks 表 一段文字一列 屬於一份 document

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from app.core.db import Base


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    # 這段文字轉成的向量 用 pgvector 的 Vector 型別 才能在 SQL 裡用 <=> 做相似度搜尋
    # 1536 對應 text-embedding-3-small 這個 embedding 模型輸出的維度 兩邊要對得上
    embedding = Column(Vector(1536), nullable=False)

    # chunk.document 拿到這段文字屬於的 Document 物件
    # back_populates="chunks" = Document 那邊對回來的屬性名 (Document 要有一條 chunks relationship 指回這裡)
    # "Document" 用 class 名字串寫法避免 import 順序問題
    document = relationship("Document", back_populates="chunks")
