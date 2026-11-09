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
| 問答時資料庫的查詢沒有等待上限 | 改之前就是這樣：50 秒的預算只管 OpenAI 的兩次呼叫，資料庫很慢時（例如連線卡住）整個請求還是可能超過 CloudFront 的 60 秒。B3-3 跑完整測試時實際遇過一次：遠端測試資料庫的一次搜尋卡了 88 秒，之後預算用完、程式照設計不送生成答案、回 503（測試在等的是生成失敗，所以失敗了；那兩個測試後來把預算放大，不受資料庫速度影響）。另外，資料庫出錯時 `finally` 寫用量也會失敗（session 要先 rollback），那次轉向量的用量會漏記（金額極小） | 還沒處理（不在 B3 範圍）。要處理的話是資料庫連線加 `connect_timeout`、`statement_timeout` |
| 請求超時或回應遺失時的費用 | 用量只能從成功收到的回應裡拿到。請求超時、連線在回應前斷掉的話，OpenAI 可能已經處理（也可能收了錢），我們這邊確認不了，也不會記到用量；SDK 重試會再送一次 | 接受（B3-1 修正說明，不另外做計費對帳）。要對帳的話看 OpenAI 後台的用量 |
| 舊的工單在使用者按重試之後才出現 | 例如上一輪的 worker 當掉，SQS 1 小時後重送；或工單其實送到了、回應遺失。跟重試送出的新工單是同一份文件，先到的搶到租約處理、後到的跳過，不會處理兩次（租約只給排隊中的文件，B2-2）。剩下的限制：可能是舊工單搶先處理，log 裡帶的是舊請求的編號；先到的那次失敗的話，後到的也會跳過，要再按一次重試。不打算加工單版本號 | 接受（B2-3 確認） |

## 雲端（部署之後才能驗證）

| 項目 | 為什麼要注意 | 狀態 |
|---|---|---|
| 部署時還開著舊畫面的使用者 | 舊的前端還有取消按鈕、會讀進度欄位：按取消會跳出「Not Found」（API 回 404，文件不會被改到）；進度欄位讀不到，只顯示「處理中」。重新整理就是新畫面 | 部署後確認 |
| 前端的實際操作 | B2-3 只跑了 build 和 lint，沒有實際點擊：上傳、處理中、完成、重試、刪除、舊的已取消文件的顯示都還沒驗證 | 部署後（或有安全的本機環境時）手動驗證 |
| 告警的篩選條件要跟程式一起更新 | B3-3 把問答失敗 log 的 `error_type` 從 `permanent` 改成 `OpenAIPermanentError`，`deploy/setup_monitoring.py` 的篩選條件也跟著改了，但 AWS 上的還是舊的。部署新程式和重跑 `setup_monitoring.py` 之間的幾分鐘，問答的永久性錯誤對不上（永久性錯誤會一直發生，下一次就對得上，不會漏掉整件事）。本機已經用 tests/test_alarms.py 驗過 log 跟篩選條件對得上；AWS 上的篩選語法還沒驗 | 部署時：重跑 `.venv\Scripts\python deploy/setup_monitoring.py 你的email`。想先驗語法、不改任何設定的話：`aws logs test-metric-filter --filter-pattern '{ $.event = "qa_failed" && $.error_type = "OpenAIPermanentError" }' --log-event-messages '{"event": "qa_failed", "error_type": "OpenAIPermanentError"}'`，回傳裡有一筆符合就對了 |
| SDK 重試的 log 有沒有進到 CloudWatch | B3-1 之後轉向量的重試由 SDK 寫 INFO「Retrying request in …」，`log.setup()` 把它接成我們的 JSON。本機和測試確認過，Lambda 上還沒看過 | 部署後確認：worker 的 log 查得到這句、帶 document_id |
| 心跳的期限 10 分鐘沒有在 AWS 上實測 | 330 秒（B3-1 之後：3 次 × 30 秒 + 2 次等待 × SDK 最多照等的 120 秒）是假設「每次請求在 30 秒內結束」的估算，不是保證。等待上限是 httpx 每一次讀取的上限，不是整個請求的總時間：回應一直很慢地傳，一批還是可能更久。S3 在異常時，boto3 的等待上限加預設的重試，下載也可能更久。被誤判時存結果會被擋下來，資料不會錯，但會白做一次、顯示「處理中斷」 | 部署後量：一份大文件從取得租約到每次報平安的間隔（log 的 claimed、chunked、openai_usage 的時間差） |
| worker 的 `sqs:ChangeMessageVisibility` 權限 | B1 之後用不到了。要先部署新程式、確認沒問題，再收回權限 | 部署後 |
| Lambda 處理到一半被中斷 | 改成 10 分鐘沒報平安才標中斷，原本的演練 3 是 5 分鐘 | 部署後重做演練 3 |

## 其他

| 項目 | 狀態 |
|---|---|
| A2～A6 的可讀性整理（問答服務、前端、eval、train、零碎） | B 做完之後 |
| pytest 跑一次約 20 分鐘（每個測試都在遠端的 Neon 建表、刪表） | 可以改成跟 CI 一樣用本機 Docker 的 PostgreSQL，但 conftest 要先加「用環境變數指定測試資料庫」的開關 |
