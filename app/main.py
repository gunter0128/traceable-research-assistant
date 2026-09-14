from fastapi import FastAPI

from app.api import auth, documents, workspaces

app = FastAPI()
app.include_router(auth.router)
app.include_router(workspaces.router)
app.include_router(documents.router)


@app.get("/")
def get():
    return {"ok": True}