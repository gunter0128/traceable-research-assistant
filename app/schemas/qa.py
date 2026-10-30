# 問答相關的 API 進出資料形狀

from pydantic import BaseModel


class AskRequest(BaseModel):
    # POST /workspaces/{workspace_id}/ask 收這個
    question: str


class CitationOut(BaseModel):
    # 答案裡的 [number] 指的是哪一份文件的哪一段內容（docs/decisions/021）
    number: int
    document_id: int
    filename: str
    content: str


class AskResponse(BaseModel):
    # 回給客戶端的：LLM 生成的答案 + 答案裡真的標到的來源（逐句引用，docs/decisions/021）
    # 說找不到時是空的；模型有回答卻忘了標編號時，退回列出搜到的全部段落（qa_service.ask_with_passages）
    answer: str
    citations: list[CitationOut]
