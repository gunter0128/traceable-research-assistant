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
    # 答案沒標任何來源時（例如「文件中找不到相關資訊」）是空的，不會把搜到但沒用到的段落當成依據
    answer: str
    citations: list[CitationOut]
