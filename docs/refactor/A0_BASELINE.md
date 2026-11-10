# A0：重構前的基準紀錄

日期：2026-11-09

Cloud Core 重構（`refactor/cloud-core` 分支）開始之前，程式在這個狀態下的測試與檢查結果。之後每一個階段改完，都跟這裡比：測試清單、通過的測試、比對工具的輸出都要一樣（會改變行為的 B 階段，只有刻意改寫的測試可以不同）。

## 基準點

| | |
|---|---|
| 分支 | `main` |
| Commit | `41a3abc`（docs: record that starting the models from docker-compose is on hold） |
| 工作目錄 | 乾淨：沒有未提交、未追蹤的檔案，沒有 stash |

A1（測試整理，commit `35b8c79`）之後重跑：測試清單（名稱、順序）、通過的測試、每個檔案的 assert 數量都跟這裡一樣。

## 跑測試之前的安全確認

測試會在資料庫上 `create_all` / `drop_all`（每個測試開始前建表、結束後刪表），連錯資料庫就會把資料表刪掉，所以先確認：

1. `.env`、`.env.test` 都被 `.gitignore` 排除，沒有進 git
2. `tests/conftest.py` 先讀 `.env.test`、把 `DATABASE_URL` 寫進環境變數，才 import app：測試和 app 用的是同一個測試資料庫
3. `.env.test` 跟 `.env` 是不同的 Neon 端點（同一個 Neon 專案的另一個 branch）
4. 用唯讀連線（`default_transaction_read_only=on`）看測試資料庫：沒有任何應用程式的資料表，只剩 `alembic_version`，不是正式資料庫
5. `.env` 沒有 `SQS_QUEUE_URL`：測試走本機模式，不會真的送工單；用到 SQS 的測試都換成假的佇列

跑測試時另外用環境變數把 OpenAI、AWS 的金鑰蓋成假的（環境變數比 `.env` 優先），跟 CI 一樣：就算哪個測試漏了假函式，也只會因為金鑰錯誤而失敗，不會真的連出去、花錢。

## 執行指令

在專案資料夾，Git Bash：

```bash
# 測試清單
OPENAI_API_KEY=sk-test-dummy AWS_ACCESS_KEY_ID=test-dummy AWS_SECRET_ACCESS_KEY=test-dummy S3_BUCKET_NAME=test-bucket \
  .venv/Scripts/python.exe -m pytest --collect-only -q -p no:warnings

# 全部的測試
OPENAI_API_KEY=sk-test-dummy AWS_ACCESS_KEY_ID=test-dummy AWS_SECRET_ACCESS_KEY=test-dummy S3_BUCKET_NAME=test-bucket \
  .venv/Scripts/python.exe -m pytest -q -p no:warnings

# Ruff（CI 檢查的範圍是 app、tests）
.venv/Scripts/python.exe -m ruff check app tests

# 前端（在 frontend/）
npm run build
npm run lint
```

PowerShell 的話，先 `$env:OPENAI_API_KEY='sk-test-dummy'`（其他三個一樣），再跑 `.venv\Scripts\python -m pytest -q -p no:warnings`。

## 結果

### pytest：156 個全部通過

約 23 分半：每個測試都在遠端的 Neon 建表、刪表，所以慢。

| 測試檔 | 數量 | 測試檔 | 數量 |
|---|---|---|---|
| test_auth.py | 6 | test_lease.py | 15 |
| test_background_processing.py | 14 | test_logging.py | 7 |
| test_chunk_repo.py | 4 | test_model_endpoints.py | 6 |
| test_core.py | 10 | test_openai_errors.py | 24 |
| test_document_processing.py | 7 | test_qa.py | 17 |
| test_documents.py | 13 | test_storage_consistency.py | 13 |
| test_duplicate_documents.py | 4 | test_usage.py | 8 |
| | | test_workspaces.py | 8 |

輸出裡的 `ERROR app:log.py ... dispatch_failed`、`unexpected_error` 這類行，是測試刻意製造錯誤時程式寫的 log，不是測試失敗。

### Ruff

`app`、`tests` 通過。CI 沒檢查的 `eval`、`train`、`deploy` 也通過。

### 前端

- `npm run build`（含 `tsc` 型別檢查）通過
- `npm run lint`：0 個錯誤、6 個警告。2 個在 shadcn 產生的 `ui/badge.tsx`、`ui/button.tsx`；4 個在 `WorkspaceList.tsx`、`WorkspaceDetail.tsx`（`load` 在宣告前被讀取、`useEffect` 的相依清單少了 `load`、`onUsageChanged`），A3 處理
- 前端沒有自動化測試

## eval / train 的比對方式

eval、train 沒有測試，改這兩個資料夾時用 [eval_train_baseline.py](eval_train_baseline.py) 比對：改之前跑一次、改完再跑一次，每一行都要一樣。

```bash
.venv/Scripts/python.exe -I docs/refactor/eval_train_baseline.py . <專案外面的輸出資料夾>
```

它只讀專案的檔案，產生的東西都寫到輸出資料夾。需要本機的 `eval/data/results/`、`train/data/taipei/`（不進 git）。

基準點的輸出（跑兩次完全一樣）：

| 項目 | 怎麼比 | 結果 |
|---|---|---|
| 評估題目（企業、勞動法規各 70 題） | 重新產生 `questions.jsonl` / `questions.md`，跟 git 裡的逐字元比對 | 都一致 |
| `build_pdfs.to_html` | 100 份知識庫原稿轉成的 HTML 的指紋 | `c95b5a6641c6e265` |
| `citation_checks` | 用存下來的評估結果重算 1,053 題，跟當時存的比 | 1,053 題全部一樣，指紋 `f94b71bd96c71408` |
| train 出題規劃 | `plan()` 229 個工作、pilot 18 個、`plan_more()` 213 個的指紋 | `a015489f6aff6f75`、`f6adc1f77397f92a`、`836d437d12efb5f5` |
| train 題目審查 | 重新產生審查後的題目（546 題），跟本機的 `questions_reviewed.jsonl` 比 | 一致 |

指紋會隨資料改變：之後改 eval / train 之前，用當時的程式重跑一次當作比對的起點，不要直接拿這張表的數字比。

## 還沒驗證的項目

| 項目 | 原因 |
|---|---|
| `train/train_adapter.py` | 需要 torch、peft、mlflow，只裝在實驗室的訓練環境 |
| eval 完整跑一輪 | 要開臨時資料庫、呼叫 OpenAI（評審約 0.5 美元一輪） |
| `train/make_questions.py`、`make_answers.py` 完整跑 | 要實驗室的模型 |
| 前端的實際操作 | 沒有自動化測試，要手動點過一遍 |
| 雲端（三個 Lambda、SQS、Sweeper） | 重構期間不部署；合併、部署之後另外驗證 |
