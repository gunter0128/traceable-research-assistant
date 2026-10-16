# 開機時組裝整個 app：把各功能各自寫好的 router 掛進來，變成一個完整的系統
# main.py 自己不寫業務邏輯，只負責把它們兜在一起

import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api import auth, documents, qa, workspaces
from app.core import log

log.setup()

# app = 整個系統的路由分派表，uvicorn app.main:app 就是把這個物件跑起來
app = FastAPI()


# 每個請求都會先經過這裡（docs/decisions/010）：發一個請求編號、量花多久、結束時寫一筆 request log
# @app.middleware("http")：登記一個「包在所有 endpoint 外面」的函式，call_next 就是去跑真正的 endpoint
# async def：middleware 規定要寫成非同步的，await 就是「等 endpoint 跑完」
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # 隨機 12 碼就夠分辨了。這次請求裡寫的所有 log 都會帶著它，送去 SQS 的工單也會帶著它
    request_id = uuid4().hex[:12]
    log.bind(request_id=request_id)
    start = time.perf_counter()
    fields = {"method": request.method, "path": request.url.path}
    try:
        response = await call_next(request)
    except Exception:
        # 預料之外的錯誤（會變成 500）：連同錯誤堆疊一起記下來，再往外丟讓 FastAPI 回 500
        log.event(
            "request", logging.ERROR, exc_info=True, status=500,
            duration_ms=log.elapsed_ms(start), user_id=_user_id(request), **fields,
        )
        raise
    # 回應的 header 也帶上編號：使用者回報問題時，拿這個編號就能查到這次請求的所有 log
    response.headers["X-Request-ID"] = request_id
    log.event(
        "request", status=response.status_code,
        duration_ms=log.elapsed_ms(start), user_id=_user_id(request), **fields,
    )
    return response


# 登入的請求，get_current_user 會把使用者編號放在 request.state 上；沒登入的（例如註冊）就是 None
def _user_id(request: Request) -> int | None:
    return getattr(request.state, "user_id", None)


# 全域錯誤處理：任何一個 API 遇到「連不上資料庫」（OperationalError），都回 503 和看得懂的訊息
# 不然 FastAPI 預設回 500 純文字「Internal Server Error」，前端只能顯示「request failed with status 500」
# @app.exception_handler(某種錯誤)：登記「遇到這種錯誤時，改用這個函式產生回應」，不用每個 endpoint 各自 try/except
@app.exception_handler(OperationalError)
def database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "服務暫時無法使用，請稍後再試"})

# 掛上各功能自己的 router，app 才知道哪個路徑要交給哪個檔案處理
app.include_router(auth.router)
app.include_router(workspaces.router)
app.include_router(documents.router)
app.include_router(qa.router)


# 跟三個功能無關，單純檢查伺服器有沒有活著用的
@app.get("/")
def get():
    return {"ok": True}