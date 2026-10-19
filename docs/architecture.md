# 架構筆記

> V0（Auth / Workspace / Document）、V1（RAG）、V2（部署到 AWS）已完成。2026-10-14 起不加新功能，改成把現有功能往深做（見 [decisions/001](decisions/001-往深做而不是往寬做.md)），第一項是把文件處理改成背景執行，見最後的「背景處理」一節。目前 57 個測試。這份文件記錄各部分的分工。

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

**驗證方式**：先手動端到端測試過一次（真的註冊、建 workspace、上傳一份含文字的 PDF、問一個內容相關的問題），答案內容跟 PDF 實際內容一致、citation 正確帶出來源，確認整條 pipeline 沒問題之後，才補上自動化測試：`tests/test_core.py`（`chunker`／`pdf_parser` 純函式）、`tests/test_chunk_repo.py`（向量搜尋，含 workspace 隔離、排序、limit）、`tests/test_qa.py`（`/ask` 端點 + ownership），共 13 個，加上原本 V0 的 23 個，共 36 個。真的呼叫 OpenAI 的部分（embedding、生成）全部用 `monkeypatch` 換成固定假回應，測試不花錢、結果穩定，也不需要自己的 OpenAI key。

---

## V2（部署到 AWS）—— 已完成

> 這一段記的是最早的部署方式（API 跑在 ECS）。2026-10-16 全部從雪梨搬到新加坡、2026-10-17 API 改用 Lambda 並刪掉 ECS，現在的架構見下面的「目前的部署架構（C4）」。

**方向**（2026-09-22 決定，考量點是求職含金量，不是最快能動）：雲端只選 AWS 一家，不跨雲混用。物件儲存用 S3、跑程式碼用 ECS + Fargate（容器化、免管伺服器）。詳細的指令跟每個資源的用途，記在 [`docs/deployment-commands.md`](deployment-commands.md)，這裡只記整體進度跟架構決定。

**目前狀態（2026-10-06）**：

```
✓ 1. 存檔改用 S3（app/core/s3_client.py）
✓ 2. Dockerfile + .dockerignore，本機 build/run 驗證過
✓ 3. AWS 帳號、IAM（chun-admin 管理員 / trace-app-s3 本機開發用）、ECR（image 已推送）
✓ 4. Secrets Manager（DATABASE_URL / SECRET_KEY / OPENAI_API_KEY）
✓ 5. 兩個 IAM Role：
     - trace-app-execution-role（啟動容器用：抓 image、讀密鑰、送 log）
     - trace-app-task-role（app 執行期間用：碰 S3，取代 trace-app-s3 金鑰）
✓ 6. Task Definition（deploy/task-definition.json，family: trace-app）
✓ 7. Security Group（trace-app-sg，開放 inbound 8000）
✓ 8. ECS Cluster（trace-app-cluster）+ Service（trace-app-service）建立完成
  9.（可選）GitHub Actions CI/CD
```

**已知問題，已修正**：第一次建 Service 時容器一直啟動失敗（`ExitCode: 1`）。從 CloudWatch Logs 查到原因：`config.py` 的 `Settings` 把 `aws_access_key_id` / `aws_secret_access_key` 設成必填，但 Task Definition 故意沒有提供這兩個環境變數（部署版本要靠 `trace-app-task-role` 自動取得憑證，不需要金鑰）——容器一啟動，`Settings()` 驗證就失敗崩潰。修正：這兩個欄位改成選填（`str | None = None`），`s3_client.py` 改成「有金鑰就明確傳給 boto3（本機開發），沒有就讓 boto3 自動去問 Task Role 要臨時憑證（部署環境）」。改完要重新 build → push → `aws ecs update-service --force-new-deployment` 才會生效，不會自動套用到已經在跑的容器。

**部署的公有 IP 是浮動的**：每次任務重啟，IP 會換，不是固定網址。之後若要固定網址，需要加 Load Balancer，先不做（不是這階段的必要項目）。

**運作期間持續在計費**：Fargate 是只要任務在跑就按秒計費，不是按請求計費，跟 OpenAI 那種用量計費不一樣。不用的時候記得把 Service 的 Desired tasks 調成 0，不然會持續燒 AWS 的 $200 額度。**目前 Desired tasks 是 0**（端到端測試過一輪、確認部署的版本能正常跑完 register → login → 建 workspace → 上傳 PDF → 問答全流程之後，手動關掉省錢）。要重新測試，照 `docs/deployment-commands.md` 的「套用新版本」那段把 Desired tasks 調回 1 即可，不用重建任何東西。

---

## 目前的部署架構（C4，2026-10-17）

全部在新加坡（ap-southeast-1），跟 Neon 資料庫同一個區域（[decisions/011](decisions/011-搬到新加坡.md)）。API 從 ECS 改成 Lambda，前面用 CloudFront 合成一個固定的 HTTPS 網址（[decisions/013](decisions/013-固定網址與-api-改用-lambda.md)）：

```
                    CloudFront（https://dpozdeau5nd0p.cloudfront.net）
                    ┌──────────┴──────────┐
              網頁畫面                    API 請求（/auth、/workspaces、/documents、/usage）
                 │                          │
           S3（前端檔案）            Lambda：trace-app-api（FastAPI + Mangum）
                                            │
               使用者的瀏覽器 ──直接上傳──→ S3（PDF）
                                            │
                                    SQS → Lambda：trace-app-worker
                                            │
                     每小時排程 → Lambda：trace-app-sweeper
                                            │
                                  Neon 資料庫（新加坡）
```

| 元件 | 用途 |
|---|---|
| CloudFront | 固定的 HTTPS 網址；依路徑把請求轉給 API 或前端；快取前端檔案 |
| S3（前端） | `npm run build` 產生的靜態檔案，只有 CloudFront 能讀 |
| Lambda：trace-app-api | 同一份 FastAPI 程式碼，用 Mangum 轉接 Lambda 的事件格式（`app/lambda_api.py`） |
| S3（PDF） | 使用者的 PDF。瀏覽器拿 API 發的上傳許可直接傳上來，不經過 API |
| SQS + Lambda：trace-app-worker | 背景處理文件（A2） |
| Lambda：trace-app-sweeper | 每小時把卡住的文件標失敗、清孤兒檔案（007、006） |
| Secrets Manager | 資料庫網址、JWT 密鑰、OpenAI key，Lambda 啟動時讀取 |
| CloudWatch Logs | 三個 Lambda 的結構化 log，保留 30 天（010） |

三個 Lambda 用同一份 Docker image，只是啟動時呼叫的函式不同。整條線上沒有需要一直開著的機器，沒人用的時候幾乎不花錢。

上傳的流程（Lambda 一次只能收 6 MB，上限是 20 MB，所以檔案不經過 API）：

```
① 前端 → POST /workspaces/{id}/uploads {檔名, 大小} → API 檢查後回傳上傳許可（presigned POST）
② 前端 → 直接 POST 檔案到 S3（S3 依許可上的條件檢查大小、位置、期限）
③ 前端 → POST /workspaces/{id}/documents {key, 檔名} → API 確認檔案在 S3 上，建紀錄、送工單
```

部署指令見 [deployment-commands.md](deployment-commands.md) 的「從零建立整套環境」和「每次發布新版本」。

---

## 背景處理（A2）—— 已完成，雲端實測通過（2026-10-15）

原本上傳 PDF 時，解析、切段、每段呼叫 OpenAI、存資料庫全部在同一個 request 裡做完才回應，大文件會讓使用者等到連線逾時。現在 API 只收檔、建紀錄、送工單，立刻回應；真正的處理交給 Lambda 上的 worker。設計理由見 [decisions/003](decisions/003-背景處理用-sqs-加-lambda.md)、[decisions/004](decisions/004-lambda-的部署方式.md)，部署指令見 [deployment-commands.md](deployment-commands.md) 的「背景處理」一節。

### 整體流程

```
使用者上傳 PDF
     │
     ▼
API（Fargate，main.py）
     ├─ 存檔 ─────────────▶ S3
     ├─ 建紀錄（處理中）───▶ Neon
     ├─ 送工單 ───────────▶ SQS 主佇列 ──同一張失敗 3 次──▶ dead-letter queue
     └─ 立刻回應「處理中」              │
                                      │ 有工單就叫醒（每次 1 張、最多同時 2 個）
                                      ▼
                              Lambda：trace-app-worker（worker.py → handle_process_message）
                                      ├─ 讀 PDF ─────▶ S3
                                      ├─ 轉向量 ─────▶ OpenAI
                                      └─ 存 chunk、改成完成 / 失敗 ──▶ Neon

EventBridge（每小時）──▶ Lambda：trace-app-sweeper（worker.py → handle_sweep）
                              ├─ 超過 15 分鐘還在處理中的 → 標「處理逾時」──▶ Neon
                              └─ 對帳：沒有紀錄的舊檔案 → 刪掉 ──────────▶ S3

前端：有文件處理中時，每 3 秒問一次 API 狀態
```

**API 和 worker 從頭到尾不直接溝通**，只透過 SQS（「有事要做」）和資料庫（「做完了」）交換資訊。Fargate 上的 API 關著時，worker 照樣能處理。

**同一個 image，兩種進入點**：Fargate 照 Dockerfile 跑 uvicorn → `main.py`；Lambda 用 CMD 覆寫改跑 `awslambdaric` → `worker.py`。兩邊呼叫的是同一份 `document_service.process_document`。

**本機沒有 SQS**：`SQS_QUEUE_URL` 沒設定時，`dispatch_document` 直接在 request 裡呼叫 `process_document`，跟改版前一樣。

### 新增的元件

| 檔 | 職責 |
|---|---|
| `app/worker.py` | Lambda 的入口，跟 `api/` 同一種角色：拆開工單、開關 session，然後交給 `document_service` |
| `app/core/sqs_client.py` | 送工單到 SQS，工單內容只有 `document_id` |
| `app/core/lambda_secrets.py` | Lambda 冷啟動時從 Secrets Manager 讀三個密碼進環境變數（Fargate 有內建功能做這件事，Lambda 沒有） |
| `app/core/aws_clients.py` | 建 boto3 client 的共用寫法。本機、Fargate、Lambda 拿憑證的方式各不相同，規則只寫在這裡 |
| `document_service.dispatch_document` | 交出去處理：雲端送工單、本機直接處理 |
| `document_service.retry_document` | 手動重試失敗的文件（`POST /documents/{id}/retry`） |
| `document_service.expire_overdue_documents` | 每小時掃描：處理中 5 分鐘沒報平安的標「處理中斷」、排隊中 15 分鐘沒人處理的標「處理逾時」（使用者查看時也會當場檢查，見 decisions/007、008） |
| 租約（`document_repo.claim` / `heartbeat` / `finish_processed`） | Lambda 處理前先取得租約（記一個隨機代碼），處理中一直報平安，存結果時要對得上代碼才算數（decisions/008） |

### 出錯時的處理

| 狀況 | 怎麼處理 |
|---|---|
| 處理到一半失敗 | 整份文件一個 transaction，rollback，不留半套 chunk（A1） |
| 同一份文件被處理兩次 | 先刪舊 chunk 再存新的，結果一樣（冪等）；worker 收到已完成 / 已失敗的文件直接跳過 |
| 工單送不出去 | 立刻標失敗，使用者可以按重試 |
| OpenAI / S3 暫時連不上 | 秒級重試，不行就 2 分鐘後再一回合，最多 3 回合（約 10 分鐘）才標失敗（decisions/005） |
| 每次處理都讓程式當掉 | `attempts` 欄位記次數，超過 3 次就不再處理；SQS 層面同一張工單被拿 3 次也會移到 dead-letter queue |
| 程式處理到一半死掉 | 處理中的 Lambda 每一步都報平安（另一條連線立刻 commit），超過 5 分鐘沒報平安就標「處理中斷」（decisions/008） |
| 還沒開始處理就卡住 | 排隊中超過 15 分鐘還沒人取得租約就標「處理逾時」（decisions/007） |
| 兩個 Lambda 處理同一份文件 | 租約只有一個人搶得到；被判定中斷後才醒來的那個，報平安或存結果時代碼對不上，自動放棄 |
| 抓不到字、頁數或檔案太大、PDF 壞掉 | 標失敗並在 `error_message` 寫明原因，前端顯示 |

### 雲端實測（2026-10-15）

上傳回應 2.9 秒（狀態「處理中」），一頁的 PDF 在 14.7 秒後處理完成，空白 PDF 正確標成失敗並附原因，問答答對，主佇列和 dead-letter queue 都沒有殘留。兩份文件同時上傳時，兩個 Lambda 同時啟動。

已知待改：Lambda 冷啟動的準備時間約 6 秒（第一次用新 image 時超過 10 秒上限、被重試）。其中一部分是 `chunker.py` 在載入時會上網下載 tiktoken 的編碼表，每次冷啟動都要重下載，也代表下載網址連不上時 Lambda 會無法啟動。打算在 build image 時先下載好。

---

## 前端 —— 提前動工中（原計畫 V4，為了求職 demo 需求提前插進 V2）

**背景**：V2 跑起來之後，發現用 Swagger UI 打 JSON 測試太醜、不好展示，決定在部署完成之餘先插入一個極簡前端，讓這個系統看起來像「真的產品」，而不是改動 V2 本身的部署範圍。

**技術選擇**：React + Vite + TypeScript + Tailwind CSS v4，元件風格照 shadcn/ui 的標準寫法（Radix UI 當底層、CSS variable 控制主題色）。選 React 是因為業界最多人用、履歷辨識度最高；不用 Streamlit/Gradio 是因為那些是獨立 process，會多一塊部署用的基礎設施；用純 HTML 又太陽春。前端程式碼本身不是這個人求職方向要練的重點，所以這塊是直接讓 Claude 寫（跟部署那段「自己打指令」的原則不同，這段是刻意的例外）。

**目錄結構**：`frontend/` 是獨立的 Node.js 專案，跟 `app/`（Python）完全分開，自己的 `package.json`。本機開發指令、環境建置細節在 [`docs/deployment-commands.md`](deployment-commands.md) 的「前端：本機開發環境建置」那節。

**目前狀態（2026-10-07）**：

```
✓ 專案骨架（Vite + React + TypeScript + Tailwind v4）
✓ 手動撰寫 10 個 shadcn 風格元件（button/card/input/label/tabs/badge/
  scroll-area/separator/textarea/sonner）—— 原因見下方「已知問題」
✓ API 串接層（src/lib/api.ts，對應後端所有路徑）
✓ 三個畫面：登入/註冊、工作區列表（建立/刪除）、工作區詳情
  （文件上傳/列表/刪除 + QA 問答聊天視窗，附來源 citation）
✓ 開發用 proxy（vite.config.ts）：5173 的請求自動轉給本機 8000，
  不用設定 CORS
✓ 本機以「兩個開發伺服器」(5173 前端 + 8000 後端) 的方式端到端測試過，
  確認整個流程能動
  尚未整合進 Dockerfile（目前的 Dockerfile 只 build 後端）、
  尚未部署到 AWS —— 這是下一步
```

**已知問題**：官方的 `npx shadcn@latest init` 在這個專案的設定組合下會壞（寫完 `components.json` 讀不回來；`add` 指令不加 `--path` 的話,元件會被寫到一個叫 `@` 的錯誤資料夾，而不是 `src/components/ui/`）。研判是 shadcn CLI 最近把預設樣式換成「base-nova」(底層改用 Base UI、`cn` 改成一個獨立套件)，這個新版本在 Windows 上還不穩。解法跟之後加新元件的正確用法，寫在 `docs/deployment-commands.md` 裡，不在這裡重複。

**當時記下的問題**：citation 太陽春——答案跟下面列出的來源沒有對應關係，來源清單是「搜到的全部」，不是「答案真的用到的」。這在 B 主線處理了（逐句引用，docs/decisions/021），現在的做法見最後一節「問答品質（B）」。

## 問答品質（B）—— 2026-10-20

目標：讓使用者看得到每個答案的出處、適用條件和生效時間，判斷答案是不是適用於自己。用虛構的企業和學校知識庫評估，每個改動都量過效果（docs/decisions/016～024）。

### 現在的問答流程

```
qa_service.ask(db, workspace_id, owner_id, question)
  │  1. ownership 檢查、每日用量檢查
  │  2. 問題轉向量 → 搜最像的 5 段（SEARCH_LIMIT，020）
  │  3. 按文件分組：同一份文件的段落放在一起、照原文順序排（018）
  │  4. 查這幾份文件的第 0 段，取開頭 200 字（標題、生效日期通常在這裡，018）
  │  5. 組給模型看的資料：
  │       今天日期：2026 年 10 月 20 日
  │       【文件】HR-02_員工手冊_2026年版.pdf
  │       文件開頭：……生效日期：2026 年 1 月 1 日……
  │       [1] ……
  │       [2] ……
  │  6. 呼叫模型：system prompt 是 7 條通用的閱讀原則（019），問題後面再提醒一次標引用（021）
  │  7. 從答案裡抓出 [n]，只回傳被標到的段落（021）
  │       說找不到 → 不附引用；有回答但忘了標 → 退回列出搜到的段落，前端會註明
  ▼
AskResponse { answer: "……14 天[1]。", citations: [ { number: 1, filename, content, ... } ] }
```

### 上傳時擋重複文件（022）

處理文件時先算內容的 SHA-256。同一個 workspace 已經有一模一樣、處理好的文件，就標成失敗，並說明跟哪一份重複。檢查放在轉向量之前，所以重複的文件不花錢。

### 評估工具（`eval/`，不會部署）

| 檔案 | 用途 |
|---|---|
| `eval/enterprise/` | 虛構公司「青嶼科技」：18 份規章、58 題（練習題 41、考試題 17） |
| `eval/school/` | 虛構國小「澄溪國小」：8 份文件、21 題，只拿來驗證改進是不是只對企業文件有效 |
| `eval/build_pdfs.py` | 文件轉成 PDF，自動檢查規則表的關鍵字都在文件裡 |
| `eval/run.py` | 用臨時資料庫跑評估：檢索、引用用程式判斷，回答用 gpt-4o 評審逐項對清單 |

結果寫在 `docs/eval-results.md`。改到問答相關的程式時手動跑（不接 CI 的原因見 docs/decisions/024）。
