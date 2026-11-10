# Cloud Core 重構：後續要驗收的事

重構過程中發現、還沒處理完或還沒驗證的事（docs/decisions/046）。處理完的在「狀態」寫明是哪個階段、怎麼驗證的，不刪掉，留著當紀錄。

## 程式

| 項目 | 為什麼要注意 | 狀態 |
|---|---|---|
| 處理到一半文件被刪掉時，`_stop_after_losing_claim()` 的 `db.refresh(document)` 會失敗 | 文件已經不在了，refresh 讀不到會丟出錯誤：worker 被當成當掉（寫 ERROR、工單被 SQS 重送），本機模式上傳的請求回 500 | 已處理（B2-2）：停下來的地方改成 `document_repo.get_by_id`（被刪掉就是 None），本機模式回 404。測試：`test_worker_stops_quietly_when_document_deleted_while_embedding`、`test_document_deleted_just_before_saving`、`test_document_deleted_then_processing_fails`、`test_inline_upload_returns_404_when_document_deleted_while_processing` |
| 記用量失敗時，OpenAI 的費用已經花了但沒記到 | 每日上限會少算這一批。要確認：worker 不會繼續轉下一批（不再花錢），而且留下告警看得到的錯誤紀錄 | 已確認（B2-2）：只轉了那一批、文件標失敗、寫一筆 ERROR 的 `unexpected_error`（附錯誤堆疊）。測試：`test_usage_record_failure_stops_embedding_and_logs_error`。少算的那一批沒有補記：要補的話，得在記用量失敗時另外把用量寫進 log，再人工對帳 |
| 刪除文件剛好碰上 worker 最後 commit 的那一瞬間，可能因為外鍵回 500 | 改之前就有的情況，機率很低：worker 存結果時鎖住文件那一列，刪除等它 commit 完才繼續，這時 chunks 已經存進去，刪文件違反外鍵。結果是刪除回 500、文件留著，再按一次刪除就好。要修的話是 chunks 的外鍵加 `ondelete="CASCADE"`（要 migration）。B2-3 拿掉取消之後，「不想等」只能用刪除，碰到的機會比以前多一點 | 待決定（還沒修） |
| 前端的進度、取消 | B2-1 之後進度不再更新（畫面只顯示「處理中」）；取消按鈕還在 | 已處理（B2-3）：API 回應拿掉進度欄位、取消的 API 和按鈕移除；以前取消的文件照常顯示、可以重試 |
| 餘額不足的 429 會被 SDK 重試 | SDK 只看狀態碼，分不出餘額不足和速率限制（B3-1）。多等約 1.5 秒、多送 2 次請求，最後還是歸類成永久性錯誤、告警照樣觸發 | 接受（B3-1）。測試：`test_insufficient_quota_ends_as_permanent_error` |
| 問答還在用自己寫的重試 | `_with_retries`、`_backoff_seconds`、`_retry_after_seconds`、`give_up_on_long_wait`、`deadline`、問答的 `on_usage` callback 還在 | 已處理：B3-2 問答不重試、用量改成回傳值、預算改由 qa_service 算；B3-3 刪掉 `_with_retries` 和它的 helper、`openai_retry` 的 log、test_openai_errors.py 那段測試 |
| S3 的永久性錯誤沒有告警 | 改之前就是這樣：S3 沒權限、bucket 設錯（例如 IAM 改壞了）時，處理文件會標「系統設定有誤」、上傳會回 503，但只寫 WARNING／INFO，不會寄信。OpenAI 的永久性錯誤有告警，S3 沒有 | 還沒處理（不在 B3 範圍）。要加的話：worker 的 `failed` 且 `error_type = "StoragePermanentError"` 再一條篩選條件，API 確認上傳時也要先寫 log |
| **效能：問答的搜尋曾經卡 88 秒** | B3-3 跑完整測試時，遠端測試資料庫（Neon）的一次向量搜尋（`qa_search`）花了 87,975 毫秒，平常是幾十毫秒。之後預算用完，程式照設計不送生成答案、回 503。**原因還不知道**：可能是 Neon 那邊、網路、或連線本身卡住（同一時段 DNS 正常）。那兩個測試把預算放大，只是讓測試不受資料庫速度影響，**實際的問題沒有解決**。正式環境是否也會發生，要看部署後的 `qa_search` 耗時 | 還沒處理。部署後：用 CloudWatch Logs Insights 看 `qa_search` 的 `duration_ms` 分布（最大值、p99）；要防的話見下一列 |
| 問答時資料庫的查詢沒有等待上限 | 改之前就是這樣：50 秒的預算只管 OpenAI 的兩次呼叫，資料庫很慢時（例如上面的 88 秒）整個請求還是可能超過 CloudFront 的 60 秒，使用者看到 CloudFront 的錯誤而不是我們的訊息。另外，資料庫出錯時 `finally` 寫用量也會失敗（session 要先 rollback），那次轉向量的用量會漏記（金額極小） | 還沒處理（不在 B 範圍）。要處理的話是資料庫連線加 `connect_timeout`、`statement_timeout` |
| 請求超時或回應遺失時的費用 | 用量只能從成功收到的回應裡拿到。請求超時、連線在回應前斷掉的話，OpenAI 可能已經處理（也可能收了錢），我們這邊確認不了，也不會記到用量；SDK 重試會再送一次 | 接受（B3-1 修正說明，不另外做計費對帳）。要對帳的話看 OpenAI 後台的用量 |
| 舊的工單在使用者按重試之後才出現 | 例如上一輪的 worker 當掉，SQS 1 小時後重送；或工單其實送到了、回應遺失。跟重試送出的新工單是同一份文件，先到的搶到租約處理、後到的跳過，不會處理兩次（租約只給排隊中的文件，B2-2）。剩下的限制：可能是舊工單搶先處理，log 裡帶的是舊請求的編號；先到的那次失敗的話，後到的也會跳過，要再按一次重試。不打算加工單版本號 | 接受（B2-3 確認） |

## 雲端（部署之後才能驗證）

| 項目 | 為什麼要注意 | 狀態 |
|---|---|---|
| 部署時還開著舊畫面的使用者 | 舊的前端還有取消按鈕、會讀進度欄位：按取消會跳出「Not Found」（API 回 404，文件不會被改到）；進度欄位讀不到，只顯示「處理中」。重新整理就是新畫面 | 部署後確認 |
| 前端的實際操作 | B2-3 只跑了 build 和 lint，沒有實際點擊：上傳、處理中、完成、重試、刪除、舊的已取消文件的顯示都還沒驗證 | 已驗證（2026-11-10，在實驗室的地端版上點，前端程式跟雲端同一份）：上傳 → 處理中自己變完成、問答和引用卡片、問答時按取消、重複上傳標失敗、刪除、對失敗的那份按重試 → 處理完、再問一次都正常。還沒看過：以前取消的文件的顯示（實驗室沒有這種文件）；雲端網站沒有點（要花 OpenAI 的錢） |
| 告警的篩選條件要跟程式一起更新 | B3-3 把問答失敗 log 的 `error_type` 從 `permanent` 改成 `OpenAIPermanentError`。B4 讓問答那條篩選條件暫時兩種都接受，所以**先更新告警、再部署程式**就不會有空窗。本機已經用 tests/test_alarms.py 驗過新舊兩種 log 都對得上；**AWS 是否接受這個寫法（括號加 `\|\|`）還沒驗** | 已處理（2026-11-10 部署）：AWS 驗過語法，只更新了問答那一條篩選條件，再部署程式（docs/operations-log.md）。新程式穩定之後可以把 `permanent` 拿掉（留著也沒有壞處） |
| 用 AWS 驗篩選條件的語法 | `aws logs test-metric-filter` 只拿篩選條件去比對你給的幾筆 log，不會建立或修改任何東西 | 部署前在專案資料夾執行：`aws logs test-metric-filter --cli-input-json file://deploy/test-metric-filter-openai-permanent.json --region ap-southeast-1`。篩選條件和 3 筆範例 log 放在檔案裡（PowerShell 直接打 JSON 會把雙引號吃掉）；檔案的篩選條件跟 setup_monitoring.py 一模一樣（tests/test_alarms.py 會擋）。回傳的 `matches` 裡應該只有 `eventNumber` 1、2（新、舊寫法），沒有 3（暫時性錯誤）；語法不接受的話指令會直接報錯 |
| 部署後的健康檢查 | B4 把 worker 的 probe 換成 `{"health_check": true}`（真的查一次 `SELECT 1`），CI 改成檢查回傳的 `out.json` 是不是 `{"database": "ok"}`。本機測過：連得到回成功、連不上丟錯誤、CI 的判斷條件只認成功的回傳（tests/test_health_check.py）。CI 的這一步還沒在 GitHub Actions 上真的跑過 | 已驗證：2026-11-10 手動部署時在 AWS 上驗過；合併到 main 後 GitHub Actions 第一次真的跑這一步（run 42734293704），worker 回 `{"database": "ok"}`、判斷成功，接著 `/auth/me` 第 1 次就回 401 |
| 正式環境跑的程式不在 main 上 | 2026-11-10 從 `refactor/cloud-core` 手動部署（commit `0911026`）。之後有人 push 到 main，CI 會把 main 的舊版部署回來 | 已處理（2026-11-10）：PR #1 合併到 main，CI/CD 部署 main 的 `e30cbb2`（docs/operations-log.md） |
| SDK 重試的 log 有沒有進到 CloudWatch | B3-1 之後轉向量的重試由 SDK 寫 INFO「Retrying request in …」，`log.setup()` 把它接成我們的 JSON。本機和測試確認過，Lambda 上還沒看過 | 部署後確認：worker 的 log 查得到這句、帶 document_id |
| 心跳的期限 10 分鐘沒有在 AWS 上實測 | 330 秒（B3-1 之後：3 次 × 30 秒 + 2 次等待 × SDK 最多照等的 120 秒）是假設「每次請求在 30 秒內結束」的估算，不是保證。等待上限是 httpx 每一次讀取的上限，不是整個請求的總時間：回應一直很慢地傳，一批還是可能更久。S3 在異常時，boto3 的等待上限加預設的重試，下載也可能更久。被誤判時存結果會被擋下來，資料不會錯，但會白做一次、顯示「處理中斷」 | 部署後量：一份大文件從取得租約到每次報平安的間隔（log 的 claimed、chunked、openai_usage 的時間差） |
| worker 的 `sqs:ChangeMessageVisibility` 權限 | B1 之後用不到了。要先部署新程式、確認沒問題，再收回權限 | 部署後 |
| Lambda 處理到一半被中斷 | 改成 10 分鐘沒報平安才標中斷，原本的演練 3 是 5 分鐘 | 部署後重做演練 3 |

## 地端（docker-compose）

| 項目 | 為什麼要注意 | 狀態 |
|---|---|---|
| 重構後的程式在地端版能不能用 | B、A 階段都沒有改 onprem/、Dockerfile、設定的名稱（config.py），但改了 SDK 重試、問答不重試、錯誤分類、前端 | 程式相容性已驗證（2026-11-10，用假的模型服務）：筆電上用乾淨的 checkout、另一個專案名稱（`trace-onprem-check`：自己的 volume、網頁開在 18080）開整套 docker-compose，兩個模型換成模仿 vLLM API 的假服務，經過 nginx 跑 25 項檢查全部通過：前端檔案、註冊登入、上傳到 SeaweedFS、本機模式（沒有 SQS）直接處理、向量 1024 維、模型名稱、問題加 query prefix（文件段落不加）、有送 `ANSWER_EXTRA_BODY`、用量記 0 元、SDK 重試（失敗 2 次後成功，間隔 0.46、0.89 秒；3 次都失敗 → 標失敗 → 按重試處理完）、問答的轉向量和生成答案失敗時各只送 1 次、回 503、刪除。SDK 重試的 log 是 JSON、帶 document_id。`docker compose config` 驗過一般、`--profile gpu`、檢查用三種組合。驗完只刪了檢查用的 volume，原本的 `trace-onprem_*` 沒動 |
| 真的模型還沒驗 | 假服務只驗請求的格式和流程；答案的品質、速度、vLLM 實際的錯誤回應都沒驗。筆電上沒有模型檔、沒有 vLLM 的 image（不下載大型模型） | 已驗證（2026-11-10）：實驗室更新到 main（`git pull`、`docker compose up -d --build`，資料保留），接真的兩個模型（Qwen3-Embedding-0.6B、第 1 版 adapter），從網頁走完上傳、處理、問答、取消、重試、刪除，都正常。只抽問了幾題，沒有整套重考；要確認分數跟 044 一樣，得用 eval/run.py 重考 |
| 地端轉向量的重試變少 | 改之前一批最多試 5 次、照服務說的時間最多等 60 秒；改成 SDK 的 3 次、間隔約 0.5、1 秒。vLLM 重開的那段時間裡上傳的文件，會比以前快標失敗，要按重試 | 接受（B3-1，雲端、地端同一份程式）。eval/run.py 的問答有自己的重試（`ASK_RETRY_WAIT`），評估不受「問答不重試」影響 |

## 其他

| 項目 | 狀態 |
|---|---|
| A2～A6 的可讀性整理（問答服務、前端、eval、train、零碎） | 已完成（2026-11-10，每個階段一個 commit）。功能和輸出不變的驗證：問答相關測試；引用的新舊寫法用 eval、train 的 8376 段答案比對；eval 108 個歷史結果檔重算的摘要和報表相同；A0 指紋相同；train 的 evidence_retrieved 比對 5725 次相同 |
| A3 拆開的前端畫面沒有實際點擊 | 只跑了 tsc、build、lint，確認畫面上的文字都在打包後的程式裡。上傳、輪詢、處理完更新用量、重試、刪除、問答、取消問答、舊的已取消文件都還沒在瀏覽器裡操作過 | 已驗證（2026-11-10，實驗室的地端版，見「雲端」的「前端的實際操作」）。地端用量都是 0，看不出「處理完更新用量」有沒有動；以前取消的文件沒看到 |
| A5 的 train 同時呼叫模型的寫法沒有實際執行 | `make_answers.py`、`make_questions.py` 的 `pool.map(lambda ...)` 改成 `pool.submit`，要接本機的模型才跑得到，這次沒有執行（出題規劃、篩選規則有比對過） | 下次產生訓練資料時確認 |
| eval 和 train 執行時改 app 的設定 | `override_app_settings`、`build_knowledge_base` 直接改 app 模組裡的值（換模型、不檢查用量上限、PDF 從本機讀）。A4 只把它們集中、寫清楚，沒有拿掉：要拿掉得改 app 的介面（例如處理文件時可以直接給檔案內容），會動到線上的程式 | 接受（A4）。之後改 document_service 介面時再一起考慮 |
| pytest 跑一次約 25～33 分鐘（每個測試都在遠端的 Neon 建表、刪表；DNS 偶爾變慢、一次搜尋卡過 88 秒，都會讓整輪測試失敗或中斷） | 可以改成跟 CI 一樣用本機 Docker 的 PostgreSQL，但 conftest 要先加「用環境變數指定測試資料庫」的開關 |
