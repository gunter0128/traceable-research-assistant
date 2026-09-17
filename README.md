# Traceable Research Assistant

這個專案的起點是我的碩士論文（multi-hop / trace-aware RAG），目標是把論文裡的檢索概念做成一個真的能部署、能被別人操作的文件問答系統——不只是回答問題，還要讓使用者看見答案是怎麼一步步被找出來的。

現在這個版本（V0）還沒有 AI 的部分。V0 先把後端的地基打好：使用者能註冊登入、建立自己的研究工作區（workspace）、上傳 PDF 文件，而且每個人只看得到自己的東西。RAG 問答、multi-hop 檢索、前端這些是後面版本才會加上去。

## V0 做了什麼

系統分三塊功能，每一塊都走完整的一套：使用者驗證、資源的建立/查詢/修改/刪除，而且每個操作都會檢查「這是不是你的東西」——不存在回 404，存在但不是你的回 403。

- **Auth**：註冊、登入（bcrypt 雜湊密碼、JWT 簽發）、查自己的資料
- **Workspace**：使用者可以建立多個工作區，CRUD 齊全，只看得到自己名下的
- **Document**：在 workspace 底下上傳 PDF（存在本機硬碟），列表、查詢、刪除。文件本身沒有自己的擁有者欄位——擁有權是透過它所屬的 workspace 判斷的，這是刻意的設計：擁有者只有一個來源，不會兩邊資料不一致

後端分四層：`api`（收發 HTTP）→ `services`（業務規則，包括上面講的 ownership 檢查）→ `repositories`（唯一碰資料庫查詢的地方）→ `models`（資料表定義）。這樣分是因為每一層會因為不同的原因被修改——換資料庫只動 `repositories`，改規則只動 `services`，不會互相波及。細節看 [`docs/architecture.md`](docs/architecture.md)。

資料庫從一開始就用 PostgreSQL（開發環境是 [Neon](https://neon.tech)），沒有用 SQLite 過渡，因為 V1 要用到 Postgres 專屬的 pgvector 做向量檢索。

## Tech Stack

Python 3.11、FastAPI、PostgreSQL + SQLAlchemy 2.0 + Alembic、bcrypt + JWT、pytest。

## 本地啟動

```bash
git clone https://github.com/gunter0128/traceable-research-assistant.git
cd traceable-research-assistant
python -m venv .venv
.venv\Scripts\activate        # Windows，macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt
```

複製 `.env.example` 成 `.env`，填入你的 Postgres 連線字串和一組密鑰（`python -c "import secrets; print(secrets.token_urlsafe(32))"` 可以生一組），然後：

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

打開 `http://localhost:8000/docs` 用 Swagger UI 直接試。

## API

| 方法 | 路徑 | 說明 | 需要登入 |
|---|---|---|---|
| POST | `/auth/register` | 註冊 | |
| POST | `/auth/login` | 登入，拿 JWT | |
| GET | `/auth/me` | 查自己的資料 | ✓ |
| POST | `/workspaces` | 建立 workspace | ✓ |
| GET | `/workspaces` | 列出自己的 workspace | ✓ |
| GET | `/workspaces/{id}` | 查一個 workspace | ✓ |
| PUT | `/workspaces/{id}` | 改名 | ✓ |
| DELETE | `/workspaces/{id}` | 刪除 | ✓ |
| POST | `/workspaces/{id}/documents` | 上傳 PDF | ✓ |
| GET | `/workspaces/{id}/documents` | 列出 workspace 裡的文件 | ✓ |
| GET | `/documents/{id}` | 查一份文件 | ✓ |
| DELETE | `/documents/{id}` | 刪除文件 | ✓ |

## 測試

```bash
pytest tests/ -v
```

23 個測試，涵蓋三個功能各自的正常流程、錯誤處理，和跨使用者的 ownership 檢查（一個使用者不該碰得到另一個使用者的 workspace 或文件）。測試連的是另一個獨立的 Neon 分支，不會碰到開發用的資料，設定方式見 `.env.test.example`。

## 接下來

V1 要把 RAG 接上去：PDF 解析、chunking、embedding、pgvector 向量檢索、呼叫 LLM 生成答案並附上引用來源。再之後是部署（V2）、multi-hop trace 視覺化（V3，論文的核心差異化特色）、前端（V4）。
