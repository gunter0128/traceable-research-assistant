# 架構

系統現在長什麼樣子：每個資料夾負責什麼、一個請求怎麼流過去、部署在哪裡。為什麼這樣設計，見各節標的決策紀錄（`decisions/`，[索引](decisions/README.md)）；開發過程的經過在 git 歷史和決策紀錄裡，這裡不記。

同一份程式碼，兩種部署：

| | 公開展示版 | 企業內部版（地端） |
|---|---|---|
| 放在哪 | AWS（Lambda、CloudFront） | 公司內部的一台 GPU 主機 |
| 轉向量 | OpenAI text-embedding-3-small（1536 維） | Qwen3-Embedding-0.6B（1024 維，035） |
| 回答 | OpenAI gpt-4o | Qwen3.5-9B + 自己訓練的 LoRA adapter，不思考、temperature 0（036、040、042） |

兩邊只差設定（037）：`EMBEDDING_*`、`ANSWER_*` 不設定就是 OpenAI；設定了網址，就改接公司內部用 vLLM 架的服務，呼叫方式跟 OpenAI 一樣。

---

## 資料夾分工

| 資料夾 | 負責什麼 | 部署嗎 |
|---|---|---|
| `app/` | 線上系統（FastAPI），分層見下一節 | 是，三個 Lambda 用同一份 |
| `alembic/` | 資料表結構的版本（migration） | 部署前跑 |
| `tests/` | 自動化測試。OpenAI、S3、SQS 都換成假的，不花錢、不用金鑰 | 否 |
| `eval/` | 考試：兩套考卷、評分程式（017） | 否 |
| `train/` | 訓練回答模型：出題、寫示範答案、篩選、LoRA 訓練（039、040） | 否，在實驗室的 GPU 上跑 |
| `deploy/` | AWS 的設定檔（JSON）、建告警的腳本，指令見 [deployment-commands.md](deployment-commands.md) | — |
| `onprem/` | 地端版的 docker-compose：資料庫、檔案儲存（SeaweedFS）、API、網頁、兩個模型（045） | 是，公司的 GPU 主機 |
| `frontend/` | 網頁（React + Vite + TypeScript + Tailwind） | 是，放在 S3、經過 CloudFront |
| `docs/` | 文件：這份、決策紀錄、評估結果、故障清單、部署指令、維運紀錄 | — |

`app/` 裡面：

| | 負責什麼 |
|---|---|
| `main.py` | 組裝 FastAPI：掛上各功能的 router、每個請求寫一筆 log（010）、資料庫連不上時回 503 |
| `lambda_api.py`、`worker.py` | Lambda 的進入點：API、背景處理文件、每小時的掃描。只負責拆開 Lambda 給的資料，邏輯都在 services |
| `api/` | HTTP 進出：收請求、呼叫 service、把錯誤翻成狀態碼。共用的翻譯在 `api/errors.py` |
| `services/` | 業務規則：誰可以碰什麼、上傳和背景處理的流程、問答（包括給模型的回答原則）、用量上限 |
| `repositories/` | 只跟資料庫講話：查詢、寫入、有條件的更新（租約） |
| `models/` | 資料表長什麼樣子 |
| `schemas/` | API 進出的 JSON 長什麼樣子（跟資料表分開：例如密碼進得來、出不去） |
| `core/` | 共用的底層：設定（`config`）、資料庫連線（`db`）、log、密碼雜湊和 JWT（`security`）、PDF 解析、切段、外部服務的呼叫（`openai_client`、`s3_client`、`sqs_client`）和它們的錯誤分類 |

---

## 分層

```
     HTTP 請求                    Lambda 事件（SQS 工單、每小時排程）
         │                                 │
   main.py → api/                       worker.py
         │                                 │
         └──────────────┬──────────────────┘
                        ▼
                   services/        業務規則
                        │
                        ▼
                 repositories/      只有這層寫 SQL
                        │
                        ▼
                    models/   →   PostgreSQL + pgvector

   core/ 在旁邊，任何一層都可以用；schemas/ 掛在 api/ 旁邊
```

- 方向只往下：repository 不會呼叫 service，service 不會呼叫 api
- **誰擁有什麼只有一個判斷依據**：`Document` 沒有自己的 `owner_id`，要先查它的 workspace 是不是你的；`Chunk` 也一樣透過 document 往上找。不會出現兩邊各記一個 owner、對不起來的情況，代價是每次多查一次 workspace
- 外部服務的錯誤先在 `core/` 分成「暫時性」和「永久性」，怎麼處理（標失敗、回 503）由 service 決定（005）

---

## 上傳

API 跑在 Lambda 上，一次只能收 6 MB，上限是 20 MB，所以檔案不經過 API，瀏覽器直接傳到 S3（013）：

```
① 前端 → POST /workspaces/{id}/uploads {檔名, 大小}
         API 檢查：workspace 是你的、今天用量沒到上限、是 PDF、不超過 20 MB → 回傳上傳許可（presigned POST）
② 前端 → 直接把檔案 POST 到 S3（S3 依許可上的條件檢查大小、位置、期限）
③ 前端 → POST /workspaces/{id}/documents {key, 檔名}
         API 確認檔案真的在 S3 上、key 是發給這個 workspace 的 → 建紀錄（排隊中）→ 送工單到 SQS → 立刻回應
```

順序是「先有 S3 的檔案、再建紀錄」（006）：中途失敗只會留下沒人用的檔案，每小時的對帳會清掉，不會留下「看得到卻打不開」的紀錄。刪除時反過來，先刪紀錄再刪檔案，理由一樣。

## 背景處理

```
SQS 工單 ──▶ Lambda：worker（worker.handle_process_message → document_service.process_document）
               1. 取得租約（同一份文件同時只有一個 Lambda 能處理，008）
               2. 從 S3 下載 → 算內容的 SHA-256，跟同一個 workspace 已處理好的一樣就標失敗（022）
               3. 解析 PDF → 切段（500 token、重疊 50）→ 每 100 段送去轉向量 → 存進 chunks 表
                  每次呼叫 OpenAI 前都報平安、更新進度
               4. 「所有 chunk + 狀態改成完成」在同一個 transaction 一起生效

EventBridge 每小時 ──▶ Lambda：sweeper（worker.handle_sweep）
               把卡住的文件標失敗、清掉 S3 上沒有紀錄的舊檔案
```

本機沒有設定 SQS 時，上傳完直接在同一個請求裡處理，不用架佇列也能開發（003）。

| 狀況 | 怎麼處理 |
|---|---|
| 處理到一半失敗 | 整份文件一個 transaction，rollback，不留半套 chunk |
| 同一份文件被處理兩次 | 先刪舊 chunk 再存新的，結果一樣；已完成、已失敗的文件搶不到租約，直接跳過 |
| OpenAI / S3 暫時連不上 | 秒級重試，還是不行就標失敗「…暫時無法使用，請稍後按重試」，使用者按重試（046；原本 005 是叫 SQS 2 分鐘後再送，最多 3 回合） |
| Lambda 處理到一半死掉 | 超過 5 分鐘沒報平安就標「處理中斷，請按重試」（008） |
| 工單送出去之後一直沒人處理 | 排隊中超過 15 分鐘就標「處理逾時，請按重試」（007） |
| 使用者按取消 | 收回租約；正在處理的 Lambda 下次報平安時發現不是自己的了，自己停下來（009） |
| 掃描檔、超過 300 頁、壞掉的 PDF | 標失敗並寫明原因，前端顯示 |

完整的故障清單和演練紀錄見 [failure-modes.md](failure-modes.md)。

## 問答

`qa_service.ask`：

```
1. workspace 是你的、今天用量沒到上限（012）
2. 問題轉向量 → 搜最像的 5 段（020），只搜處理完成的文件
3. 同一份文件的段落放在一起、照原文順序排；前後相連的段落合併成一段（018、043）
4. 查這幾份文件的開頭 200 字（標題、生效日期通常在這裡，018）
5. 組給模型看的資料：
     今天日期：2026 年 10 月 20 日
     【文件】HR-02_員工手冊_2026年版.pdf
     文件開頭：……生效日期：2026 年 1 月 1 日……
     [1] ……
     [2] ……
6. 呼叫模型：system 是 7 條通用的閱讀原則（qa_service.ANSWER_RULES，019），問題後面再提醒一次標引用（021）
7. 從答案裡抓出 [n]，只回傳被標到的段落（021）
     說找不到 → 不附引用；有回答但忘了標 → 退回列出搜到的段落，前端會註明
```

整個問答共用 50 秒的時間預算：CloudFront 最多等 60 秒，留 10 秒給冷啟動（013）。轉向量成功、生成答案失敗的話，轉向量的錢還是會記進用量。

---

## 部署（公開展示版）

全部在新加坡（ap-southeast-1），跟資料庫（Neon）同一個區域（011）：

```
                    CloudFront（https://dpozdeau5nd0p.cloudfront.net）
                    ┌──────────┴──────────┐
              網頁畫面                    API 請求（/auth、/workspaces、/documents、/usage）
                 │                          │
           S3（前端檔案）            Lambda：trace-app-api（FastAPI + Mangum）
                                            │
               使用者的瀏覽器 ──直接上傳──→ S3（PDF）
                                            │
                                    SQS → Lambda：trace-app-worker ──失敗 3 次──▶ dead-letter queue
                                            │
                     每小時排程 → Lambda：trace-app-sweeper
                                            │
                                  Neon：PostgreSQL + pgvector
```

| 元件 | 用途 |
|---|---|
| CloudFront | 固定的 HTTPS 網址；API 的路徑轉給 Lambda、其他轉給前端的 bucket |
| 三個 Lambda | 同一份 Docker image，只是啟動時呼叫的函式不同（`deploy/*-image-config.json`，004） |
| Secrets Manager | 資料庫網址、JWT 密鑰、OpenAI key，Lambda 冷啟動時讀進來（`core/lambda_secrets.py`） |
| CloudWatch | 結構化 log 保留 30 天（010）；告警寄 email、帳單上限（015） |
| GitHub Actions | 每次 push 跑 lint 和測試；main 分支只部署有改到的部分，用 OIDC 拿臨時權限（014） |

整條線上沒有需要一直開著的機器，沒人用的時候幾乎不花錢。

## 企業內部版（地端）

```
qa_service.ask
  ├─ openai_client.embed_query     ──▶ vLLM：Qwen3-Embedding（問題前面加任務說明）
  ├─ chunk_repo.search_similar     ──▶ PostgreSQL + pgvector（1024 維）
  └─ openai_client.generate_answer ──▶ vLLM：Qwen3.5-9B + LoRA adapter（不思考、temperature 0）
```

向量欄位的維度跟著 `EMBEDDING_DIMENSION`（migration `e5a1b7c9d2f4`）。

整套用 docker-compose 打包（`onprem/`，045），一個指令開起來：

```
公司的 GPU 主機（只開給主機自己，127.0.0.1:8080）
 └─ nginx（web）：前端檔案；API 的路徑轉給 api；/storage 轉給 storage
      ├─ api：同一份程式、同一個 Dockerfile，沒設定 SQS → 上傳完直接處理
      │     ├─ db：PostgreSQL + pgvector
      │     ├─ storage：SeaweedFS，跟 S3 用法一樣（S3_ENDPOINT_URL）
      │     └─ vllm-embed、vllm-answer（--profile gpu 才開；沒開的話連設定的網址）
      └─ storage-init：只跑一次，建好放文件的 bucket
```

跟公開展示版只差設定：檔案存 SeaweedFS 不是 S3（上傳許可的網址換成 `/storage`，由 nginx 轉過去）、沒有 SQS 和 sweeper。300 頁的 PDF 處理約 9 秒，所以上傳時直接處理就夠了。目前在實驗室的主機上跑，從筆電用 SSH 通道看。模型是主機上手動開的兩個 vLLM：讓 docker-compose 開模型要再多約 20GB 的硬碟，共用主機的空間不夠，先暫停（045）。

---

## 考試（`eval/`）

| 檔案 | 用途 |
|---|---|
| `enterprise/` | 虛構公司「青嶼科技」：78 份文件（含舊版、子公司、草案等干擾文件，026）、70 題（練習題 48、測試題 22）。`rules.py` 是規則表，題目要算的數字從這裡算 |
| `labor/` | 真實的勞動法規：22 部（全國法規資料庫，`fetch_laws.py` 下載）、70 題（練習題 46、測試題 24，031） |
| `build_pdfs.py` | 把知識庫的 Markdown 原稿轉成 PDF：使用者上傳的是 PDF，用 PDF 才測得到解析的問題 |
| `run.py` | 用臨時資料庫跑評估：每份 PDF 走線上一樣的處理流程，逐題呼叫 `qa_service`，檢索、引用用程式判斷，回答用 gpt-4o 評審逐項對清單 |
| `common.py` | 兩套考卷共用的題目格式、引用對得上的檢查（038） |

整理過的結果在 [eval-results.md](eval-results.md)，每次評估的原始紀錄在 [eval-log.md](eval-log.md)。改到問答相關的程式時手動跑，不接 CI：要花錢，分數也會隨機波動（024）。

## 訓練回答模型（`train/`）

訓練資料用臺北市政府的內部規章，跟考卷不重複（039）。出題、寫答案、檢查全部在本機：企業拿自己的機密文件做訓練資料時，也不能送到外部。

```
collect_taipei.py   下載臺北市政府規章
      ▼
make_questions.py   Qwen（思考）讀規章出題、寫標準答案，程式檢查原文和數字
      ▼
question_review.py  人工逐題審查的結果：刪掉、修正有問題的題目
      ▼
make_answers.py     用線上一樣的 qa_service.retrieve 搜段落 → Qwen（思考）照理想回答的寫法寫示範答案
                    → 篩選：評審判全對、引用對得上、不矛盾、只用繁體中文、200 字以內
      ▼
train_adapter.py    在 RTX 3090 上訓練 LoRA adapter（040），vLLM 直接掛上；用 MLflow 記錄每次訓練
```

`llm.py` 是問 Qwen 的共用函式：結果存起來，中斷了重跑不用重問。結果：答案從 265 字變成 92 字，正確率沒有掉；跟 gpt-4o 的比較見 [eval-results.md](eval-results.md)。
