# 架構筆記

> V0（Auth / Workspace / Document）已完成，23 個測試通過。這份文件記錄 V0 的分工，以及 V1（RAG）的規劃。

---

## 分工圖（V0）

```
              Client（瀏覽器 / /docs）
                    │ HTTP 請求
                    ▼
                 main.py ── 開機時把各功能的 router 組進 app
                    │
      ┌─────────────┼──────────────────┐
      ▼              ▼                  ▼
api/auth.py   api/workspaces.py   api/documents.py
register       CRUD                上傳 / 列表 / 查詢 / 刪除
/login/me
      │              │                  │
      ▼              ▼                  ▼
auth_service   workspace_service   document_service
                （ownership 檢查：    （ownership 檢查：
                 是不是自己的        透過 workspace_id
                 workspace）         間接判斷，見下方說明）
      │              │                  │
      ▼              ▼                  ▼
 user_repo     workspace_repo      document_repo
      │              │                  │
      └──────────────┴──────────────────┘
                    │ 用
                    ▼
                 models/        User / Workspace / Document
                    │
                    ▼
                 Neon（PostgreSQL）

  document_service 另外會寫檔到 storage/documents/（本機硬碟），
  路徑記在 Document.storage_path，DB 只存路徑不存檔案本體。

  core/（放旁邊，誰都能用）
     ├ config.py    把 .env 讀成 settings 物件
     ├ db.py        建連線、發 session、提供 Base
     └ security.py  雜湊密碼、產生 / 驗證 JWT（純計算）

  alembic/ ── 讀 models/ 的表定義 → 生 migration → 套用到 Neon
```

**怎麼讀**

- 實線箭頭 = 「呼叫 / 用到」。主流向 `api → service → repository → model → db → Neon` 往下單向。
- 三條 router 分支（auth / workspaces / documents）各自獨立走完整的四層，彼此不互相呼叫。
- `core/` 三個檔在旁邊，是共用工具，任何層都可能用。
- `schemas/` 掛在 api 旁邊（驗進出格式），`alembic/` 掛在 models 旁邊（管表結構版本），都不在主流程裡。
- 方向是單向的：`repository` 不會回頭呼叫 `service`，`service` 不會呼叫 `api`。

**Document 的 ownership 是刻意設計成間接的**：`Document` 表沒有 `owner_id` 欄位，要知道一份文件是不是使用者的，得先查它的 `workspace_id`，再查那個 workspace 是不是使用者的。這樣「誰擁有這個東西」永遠只有一個判斷依據（workspace 的 owner），不會出現文件自己記一個 owner、workspace 又記一個 owner，兩邊對不起來的情況。代價是 `document_service` 的每個操作都要多一次查 workspace。

---

## 分工表（V0）

| 檔 | 職責 |
|---|---|
| `main.py` | 開機時把各功能的 router 組進 `app`；`app` = 執行時的路由分派表 |
| `api/auth.py` | 收請求、驗 body、呼叫 service、把結果 / 錯誤翻成 HTTP 狀態碼 |
| `api/workspaces.py` | 同上，處理 workspace 的 CRUD |
| `api/documents.py` | 同上，處理文件上傳（`UploadFile`，不是 JSON body）/ 列表 / 查詢 / 刪除 |
| `services/auth_service.py` | email 不能重複、密碼先雜湊、登入成功發 token |
| `services/workspace_service.py` | workspace CRUD 的規則 + ownership 檢查（不是自己的回 403，不存在回 404） |
| `services/document_service.py` | 存檔到本機硬碟、寫 DB 記錄；ownership 檢查透過 workspace 間接判斷 |
| `repositories/user_repo.py` | 跟 DB 講話：`get_user_by_email` / `get_user_by_id` / `create_user` |
| `repositories/workspace_repo.py` | 跟 DB 講話：workspace 的 CRUD 查詢 |
| `repositories/document_repo.py` | 跟 DB 講話：document 的查詢 / 建立 / 刪除 |
| `models/` | 資料表長怎樣：`User` / `Workspace` / `Document` |
| `schemas/` | API 進出的 JSON 長怎樣（跟 model 分開，例如密碼進得來出不去） |
| `core/config.py` | 把 `.env` 讀成一個有型別的 `settings` 物件；全 app 唯一碰 `.env` 的地方 |
| `core/db.py` | `engine`、`SessionLocal`、`Base`、`get_db` |
| `core/security.py` | 純計算：`hash_password` / `verify_password` / `create_access_token` / `decode_access_token` |
| `alembic/` | model 改了 → `alembic revision --autogenerate` 生 migration → `alembic upgrade head` 套用到 Neon |
| `storage/documents/` | 上傳的 PDF 實際存放位置，`document_service` 寫入，路徑存在 `Document.storage_path` |

---

## 一個請求怎麼流過去（`POST /auth/register`）

```
client  POST /auth/register {email, password}
  │
  ▼  api/auth.py  register()
  │    FastAPI 用 UserCreate 驗 body
  │
  ▼  auth_service.register_user(db, email, password)
  │    1. user_repo.get_user_by_email(email)  ──→ Neon: SELECT ... WHERE email=?
  │       └─ 有重複 → raise EmailAlreadyRegistered ──→ register() except → HTTP 409
  │    2. security.hash_password(password)      ──→ 回 hash 字串
  │    3. user_repo.create_user(email, hash)    ──→ Neon: INSERT INTO users
  │       └─ 回新的 User
  │
  ▼  register() 拿到 User → FastAPI 套 response_model=UserOut（去掉密碼）
  │
  ▼  201  { "id": ..., "email": ..., "created_at": ... }
```

## 一個請求怎麼流過去（`POST /workspaces/{id}/documents`）

```
client  POST /workspaces/3/documents  (multipart, PDF 檔案)
  │
  ▼  api/documents.py  upload_document()
  │    get_current_user 先驗 JWT，拿到 user
  │
  ▼  document_service.upload_document(db, workspace_id=3, user, file)
  │    1. workspace_repo.get_workspace_by_id(3)     ──→ Neon: SELECT ... WHERE id=3
  │       └─ 不存在 → 404 ／ 存在但 owner_id ≠ user.id → 403
  │    2. 把 file 寫到 storage/documents/<uuid>.pdf
  │    3. document_repo.create_document(workspace_id, filename, storage_path)
  │       └─ Neon: INSERT INTO documents (status 預設 "uploaded")
  │
  ▼  201  { "id": ..., "workspace_id": 3, "filename": ..., "status": "uploaded", ... }
```

---

## V1 規劃（RAG）

**目標**：使用者針對 workspace 裡的文件提問，系統回傳答案，並附上答案是從哪份文件、哪一段找到的（citation）。

**已定的兩個設計決定**：

1. **處理流程同步觸發**：PDF 解析 → chunking → embedding 這段，寫在 upload 那個 request 裡面做完，request 沒回應前就處理完成，直接把 `status` 改成 `processed`（或失敗時改成 `failed`）。不另外開 background task 或 process 端點。好處是簡單、跟現有的 `status` 欄位設計一致；代價是檔案大或 embedding API 慢的時候，使用者要等比較久 —— 這是刻意先不處理的最佳化，之後要拆成非同步再拆。
2. **Embedding 和生成都用 OpenAI**：embedding 用 `text-embedding-3-small`，生成答案用 GPT。

**新增元件**（沿用一樣的四層 pattern，不新增新的分層方式）：

| 檔 | 職責 |
|---|---|
| `models/chunk.py` | 新表 `chunks`：屬於一個 document，存文字內容 + `embedding`（pgvector 的 `Vector` 型別） |
| `repositories/chunk_repo.py` | 跟 DB 講話：插入 chunks；之後 retrieval 要做的向量相似度搜尋（pgvector 的 `<=>` 運算子）也放這 |
| `core/openai_client.py` | 純計算，不碰 DB / HTTP：包 OpenAI SDK 呼叫，`embed_text(text)` / `generate_answer(prompt)`，跟 `security.py` 同一種角色 |
| `services/document_service.py`（擴充） | 上傳完成後多一步 `process_document()`：抓 PDF 文字 → 切 chunk → 呼叫 `openai_client.embed_text` → `chunk_repo` 存起來 → 更新 `document.status` / `processed_at` |
| `services/qa_service.py`（新檔案） | `ask(db, workspace_id, question)`：把問題 embed → `chunk_repo` 做向量搜尋抓 top-k 相關 chunk → 組 prompt 呼叫 `openai_client.generate_answer` → 回傳 answer + citations（來自哪些 chunk / document） |
| `api/documents.py` 或新的 `api/qa.py` | 新端點，例如 `POST /workspaces/{id}/ask`：收問題、呼叫 `qa_service.ask`、回 JSON |
| `schemas/qa.py` | `AskRequest`（question: str）/ `AskResponse`（answer: str, citations: list[...]） |
| `alembic/` | 新 migration：開 pgvector extension + 建 `chunks` 表 |
| `requirements.txt` | 新增 `openai`、PDF 解析套件（`pypdf` 或 `pdfplumber`）、`pgvector`（給 SQLAlchemy 用的 `Vector` 型別） |

分工圖上的位置：`qa_service` 和 `document_service` 一樣掛在 `api → service → repository → model` 這條主流向上，只是 `document_service` 多呼叫一個新的 `core/openai_client.py`（跟 `security.py` 平行，都是「側邊的純計算工具」）。`chunk_repo` 是新的一條 repository，跟 `document_repo` 平行，都碰 `chunks` / `documents` 表。

**建議的實作順序**（一步一步、每步都能單獨驗證）：

1. 裝套件 + migration：開 pgvector extension、建 `chunks` 表
2. PDF parsing：先確認能把一份 PDF 的文字抓出來（不接資料庫、不接 API，純函式先測）
3. chunking：把長文字切成一段一段
4. embedding：串 OpenAI，把一個 chunk 變成一個向量
5. 接回 `document_service`：upload 完自動跑 2~4，存進 `chunks` 表，更新 `status`
6. retrieval：給一個問題的向量，從 `chunks` 表用 pgvector 搜出 top-k
7. `qa_service`：組 prompt、呼叫 LLM 生成、整理成 answer + citations
8. `POST /workspaces/{id}/ask` 端點，串起 6+7
```
