# 開機時組裝整個 app：把各功能各自寫好的 router 掛進來，變成一個完整的系統
# main.py 自己不寫業務邏輯，只負責把它們兜在一起

from fastapi import FastAPI

from app.api import auth, documents, qa, workspaces

# app = 整個系統的路由分派表，uvicorn app.main:app 就是把這個物件跑起來
app = FastAPI()

# 掛上各功能自己的 router，app 才知道哪個路徑要交給哪個檔案處理
app.include_router(auth.router)
app.include_router(workspaces.router)
app.include_router(documents.router)
app.include_router(qa.router)


# 跟三個功能無關，單純檢查伺服器有沒有活著用的
@app.get("/")
def get():
    return {"ok": True}