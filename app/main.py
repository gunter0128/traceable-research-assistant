from fastapi import FastAPI

from app.api import auth, workspaces

app = FastAPI()
app.include_router(auth.router)
app.include_router(workspaces.router)


@app.get("/")
def get():
    return {"ok": True}