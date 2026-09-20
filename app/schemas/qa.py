# 問答相關的 API 進出資料形狀

from pydantic import BaseModel


class AskRequest(BaseModel):
    # POST /workspaces/{workspace_id}/ask 收這個
    question: str


class CitationOut(BaseModel):
    # 這個答案參考了哪一份文件的哪一段內容
    document_id: int
    filename: str
    content: str


class AskResponse(BaseModel):
    # 回給客戶端的：LLM 生成的答案 + 這次搜到、拿去問的全部來源（簡單版 citation）
    answer: str
    citations: list[CitationOut]
