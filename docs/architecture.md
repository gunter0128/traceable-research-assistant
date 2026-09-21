# 架構筆記

> V0（Auth / Workspace / Document）已完成，23 個測試通過。V1（RAG）已完成，端到端手動驗證過（真的上傳 PDF、真的問問題、答案正確且附引用），還沒有自動化測試。這份文件記錄兩者的分工。

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

**Document 的 ownership 是刻意設計成間接的**：`Document` 表沒有 `owner_id` 欄位，要知道一份文件是不是使用者的，得先查它的 `workspace_id`，再查那個 workspace 是不是使用者的。這樣「誰擁有這個東西」永遠只有一個判斷依據（workspace 的 owner），不會出現文件自己記一個 owner、workspace 又記一個 owner，兩邊對不起來的情況。代價是 `document_service` 的每個操作都要多一次查 workspace。`Chunk` 沿用同一個原則：自己沒有 `workspace_id`，要透過 `document_id` 再繞去 `documents` 表才能查到。

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
  │    4. process_document(db, document)：同一個 request 裡繼續往下（見 V1）
  │
  ▼  201  { "id": ..., "workspace_id": 3, "filename": ..., "status": "processed", ... }
```

---

## V1（RAG）—— 已完成

**目標**：使用者針對 workspace 裡的文件提問，系統回傳答案，並附上答案是從哪份文件、哪一段找到的（citation）。

**兩個設計決定**：

1. **處理流程同步觸發**：PDF 解析 → chunking → embedding 這段，寫在 upload 那個 request 裡面做完，request 沒回應前就處理完成，直接把 `status` 改成 `processed`（或失敗時改成 `failed`）。沒有另外開 background task 或 process 端點。好處是簡單、跟 `status` 欄位設計一致；代價是檔案大或 embedding API 慢的時候，使用者要等比較久 —— 這是刻意先不處理的最佳化，之後真的需要再拆成非同步。
2. **Embedding 和生成都用 OpenAI**：embedding 用 `text-embedding-3-small`（1536 維），生成答案用 `gpt-4o-mini`。

### 分工圖（V1 新增部分）

```
上傳流程（接續 V0 的 document_service.upload_document）
  document_service.process_document(db, document)
      │
      ▼
  pdf_parser.extract_text(path)      core/ 純函式：PDF → 文字（pypdf）
      │
      ▼
  chunker.chunk_text(text)           core/ 純函式：長文字 → 一段一段（tiktoken 算 token 數，帶 overlap）
      │
      ▼  對每一段
  openai_client.embed_text(piece)    core/：文字 → 向量（OpenAI embeddings API）
      │
      ▼
  chunk_repo.create(...)             repositories/：存進 chunks 表
      │
      ▼
  document_repo.mark_processed(...)  更新 document.status / processed_at


問答流程（新的一條線，跟 auth/workspaces/documents 平行）
  api/qa.py  POST /workspaces/{id}/ask
      │
      ▼
  qa_service.ask(db, workspace_id, owner_id, question)
      │  1. workspace_service.get_owned_workspace(...)   借用 ownership 檢查
      │  2. openai_client.embed_text(question)           問題轉向量
      │  3. chunk_repo.search_similar(db, workspace_id, query_embedding)
      │       join Document 篩 workspace_id → ORDER BY embedding <=> query_embedding（pgvector cosine_distance）
      │  4. 組 context（搜到的幾段內容）
      │  5. openai_client.generate_answer(question, context)   丟給 LLM，system prompt 強制「只能照資料回答」
      │  6. 組 citations（這次搜到、丟給 LLM 的 chunk 全部算數，簡單版）
      ▼
  AskResponse { answer, citations }
```

### 分工表（V1 新增）

| 檔 | 職責 |
|---|---|
| `models/chunk.py` | `chunks` 表：屬於一個 document，存 `content`（文字）+ `embedding`（pgvector 的 `Vector(1536)`） |
| `repositories/chunk_repo.py` | `create()` 存一個 chunk；`search_similar()` join `documents` 篩 `workspace_id`、依 `cosine_distance` 排序，搜出最相近的 k 筆 |
| `core/pdf_parser.py` | `extract_text(path)`：純函式，pypdf 讀 PDF、逐頁抓文字兜起來 |
| `core/chunker.py` | `chunk_text(text, chunk_size, overlap)`：純函式，用 tiktoken 依 token 數切段，段落間重疊避免關鍵字被切斷 |
| `core/openai_client.py` | `embed_text(text)`（既有）+ `generate_answer(question, context)`（新增）：純函式包 OpenAI SDK 呼叫 |
| `services/document_service.py`（擴充） | `process_document()`：抓文字 → 切段 → 逐段 embed → 存進 `chunks` → 更新 `document.status`；接在 `upload_document()` 最後一步，同一個 request 裡做完 |
| `services/qa_service.py` | `ask()`：ownership 檢查 → embed 問題 → 搜相關 chunk → 組 prompt → 呼叫 LLM → 包成 answer + citations |
| `api/qa.py` | `POST /workspaces/{id}/ask`：收問題、呼叫 `qa_service.ask`、ownership 例外翻成 404/403 |
| `schemas/qa.py` | `AskRequest`（question: str）/ `CitationOut`（document_id, filename, content）/ `AskResponse`（answer, citations: list[CitationOut]） |
| `alembic/` | migration 開 pgvector extension + 建 `chunks` 表 |

### 一個請求怎麼流過去（`POST /workspaces/{id}/ask`）

```
client  POST /workspaces/9/ask {"question": "這篇論文評估了哪些資料集？"}
  │
  ▼  api/qa.py  ask()
  │    get_current_user 先驗 JWT
  │
  ▼  qa_service.ask(db, workspace_id=9, owner_id, question)
  │    1. workspace_service.get_owned_workspace(9, owner_id)
  │       └─ 不存在 → 404 ／ 不是你的 → 403
  │    2. openai_client.embed_text(question)          ──→ 問題的向量
  │    3. chunk_repo.search_similar(db, 9, query_embedding)
  │       └─ Neon: SELECT ... FROM chunks JOIN documents ... WHERE workspace_id=9
  │                ORDER BY embedding <=> ? LIMIT 5
  │    4. 組 context："[1] ...\n\n[2] ..."
  │    5. openai_client.generate_answer(question, context)  ──→ OpenAI chat completions
  │    6. 組 citations（每個搜到的 chunk：document_id / filename / content）
  │
  ▼  200  { "answer": "...", "citations": [ {...}, {...} ] }
```

**驗證方式**：手動端到端測試過一次（真的註冊、建 workspace、上傳一份含文字的 PDF、問一個內容相關的問題），答案內容跟 PDF 實際內容一致，citation 正確帶出來源。**目前沒有 pytest 自動化測試涵蓋 V1**（跟 V0 的 23 個測試不一樣），這是已知的缺口，之後可以補。

---

## 接下來（V2）

部署。目前只在本機跑，細節還沒規劃——等真的動手再回來補這一段，原則跟這份文件從 V0 到 V1 的做法一樣：先做、驗證過、再回頭寫進文件。
