# service 丟出的錯誤 → HTTP 回應，幾個 router 共用（同樣的翻譯只寫一次）
# 每個函式都一定會 raise HTTPException：在 except 裡呼叫，FastAPI 收到就回對應的狀態碼和訊息
# NoReturn：告訴型別檢查「這個函式不會正常結束」

from typing import NoReturn

from fastapi import HTTPException

from app.services import document_service, usage_service, workspace_service

# 一個 except 一次接住好幾種錯誤：except WORKSPACE_ERRORS as exc，再交給下面的函式判斷是哪一種
WORKSPACE_ERRORS = (workspace_service.WorkspaceNotFound, workspace_service.NotWorkspaceOwner)
DOCUMENT_ERRORS = (document_service.DocumentNotFound, document_service.NotDocumentOwner)


# 404：根本沒有這個 workspace；403：有，但不是你的
def workspace_error(exc: Exception) -> NoReturn:
    if isinstance(exc, workspace_service.WorkspaceNotFound):
        raise HTTPException(status_code=404, detail="workspace not found") from None
    raise HTTPException(status_code=403, detail="not your workspace") from None


# Document 沒有自己的 owner，是不是你的，透過它的 workspace 判斷
def document_error(exc: Exception) -> NoReturn:
    if isinstance(exc, document_service.DocumentNotFound):
        raise HTTPException(status_code=404, detail="document not found") from None
    raise HTTPException(status_code=403, detail="not your document") from None


# 問答、上傳、重試共用：用量到上限時回 429 Too Many Requests（HTTP 專門表示「你用太多了」的狀態碼）
# 自己的上限跟全系統的上限分開說：全系統到了不是使用者的問題
def quota_error(exc: usage_service.QuotaExceeded) -> NoReturn:
    if exc.scope == "system":
        raise HTTPException(status_code=429, detail="系統今日用量已達上限，明天再試") from None
    raise HTTPException(status_code=429, detail="今日用量已達上限，明天再試") from None
