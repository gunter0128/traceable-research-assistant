# 故障清單

系統每一步壞掉時會發生什麼事，以及證據。設計理由見 `decisions/`。

證據的標記：**測試** = 自動化測試（括號是測試名稱）、**雲端** = 在 AWS 上實際製造故障驗證過、**—** = 還沒有證據。

> A5 進行中（2026-10-15）：列清單時找到的三個缺口已補上（S3 錯誤沒分類、上傳時 S3 失敗回 500、兩個沒測試的情況），標 ☁ 待演練的是準備在雲端驗證的。

---

## 1. 上傳（API，使用者在等）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 檢查 | 不是 PDF | 回 400，什麼都不存 | 測試（test_upload_non_pdf_rejected） |
| 檢查 | 超過 20 MB | 回 413，什麼都不存 | 測試（test_file_too_large_rejected） |
| 存 S3 | S3 暫時連不上 | 回 503「儲存服務暫時無法使用，請稍後再試」，什麼都不存 | 測試（test_upload_returns_503_when_storage_unavailable） |
| 建紀錄 | 資料庫寫入失敗 | 把剛存的 S3 檔案刪掉，回 500 | 測試（test_upload_removes_file_when_record_fails） |
| 建紀錄 | 連刪檔案也失敗 | 留下孤兒檔案，每小時對帳清掉 | 測試（test_orphan_cleanup_only_deletes_old_unreferenced_files） |
| 送工單 | SQS 送不出去 | 標失敗「排入處理佇列失敗，可以重試」 | 測試（test_upload_marks_failed_when_send_fails） |
| 送工單 | 記錄送出時間時資料庫失敗 | 紀錄停在處理中、沒送出工單；sweeper 看上傳時間，30 分鐘後重送 | 測試（test_sweep_requeues_document_without_queued_at） |
| 送工單 | 工單其實送到了，但回應遺失 | 文件被標失敗，worker 收到工單時看到不是「處理中」就跳過；使用者按重試即可 | 接受：極少見，結果是多按一次重試 |

## 2. 文件處理（Lambda worker，背景執行）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 拿工單 | 文件已經被刪掉 | 跳過 | 測試（test_worker_skips_deleted_document） |
| 拿工單 | 重複的工單（文件已處理完） | 跳過，不會重複呼叫 OpenAI | 測試（test_worker_skips_duplicate_message） |
| 拿工單 | 資料庫連不上 | 這次執行失敗，SQS 60 分鐘後重送；資料庫恢復後正常處理 | 接受：SQS 的標準行為 |
| 開始 | 已經處理 3 次 | 不處理，直接標失敗 | 測試（test_process_stops_after_max_attempts） |
| 下載 PDF | S3 暫時連不上 | 跟 OpenAI 一樣：2 分鐘後再一回合，最多 3 回合 | 測試（test_storage_temporary_error_defers_in_cloud、test_s3_errors_are_classified） |
| 下載 PDF | 檔案不見了 | 標失敗「找不到上傳的檔案，請刪除後重新上傳」，不重試 | 測試（test_missing_file_fails_with_clear_reason）；真的 S3 回的錯誤也確認會被分類成「檔案不存在」 |
| 下載 PDF | 沒權限讀檔案 | 標失敗「系統設定有誤」，不重試 | 測試（test_s3_errors_are_classified） |
| 解析 | PDF 壞掉 / 超過 300 頁 / 抓不到字 | 標失敗並寫明原因，不重試 | 測試（test_corrupt_pdf_has_reason 等三個） |
| 轉向量 | OpenAI 暫時連不上 | 秒級重試 4 次；不行就 2 分鐘後再一回合，最多 3 回合 | 測試（test_temporary_error_*）、☁ 待演練 |
| 轉向量 | OpenAI 永久性錯誤（key 無效、餘額不足） | 立刻標失敗「系統設定有誤」 | 測試（test_permanent_error_fails_immediately） |
| 存 chunk | 中途任何錯誤 | rollback，一段都不留 | 測試（test_failed_processing_leaves_no_chunks） |
| 整個執行 | Lambda 超過 10 分鐘 / 記憶體不夠被強制中斷 | 沒 commit 的資料隨連線中斷自動丟棄；次數已 +1；60 分鐘後 SQS 重送；同一張工單 3 次後移到 dead-letter queue，sweeper 看到次數用完標失敗 | 部分測試（次數上限、sweeper），☁ 待演練（可選） |
| 延後工單 | 沒有「延後」的權限 | 這次執行失敗，工單照預設 60 分鐘後重送（變慢，但不會遺失） | ☁ 待演練（權限從來沒被實際用到） |

## 3. 每小時掃描（Lambda sweeper）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 重送 | 卡住超過 30 分鐘 | 重送；次數用完的標失敗 | 測試（test_sweep_requeues_only_stuck_documents） |
| 對帳 | 孤兒檔案 | 放超過 1 天才刪，避免誤刪正在上傳的檔案 | 測試（test_orphan_cleanup_only_deletes_old_unreferenced_files）、☁ 待確認（2026-10-16 查測試用孤兒檔案有沒有被刪） |
| 對帳 | S3 列不出來 | 這次執行失敗，但重送已經先做完了；下個小時再試 | 接受 |

## 4. 問答（API，使用者在等）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 搜尋 | 只有失敗或處理中的文件 | 搜不到，回「文件中找不到相關資訊」 | 測試（test_search_similar_skips_unprocessed_documents） |
| 轉向量 / 生成 | OpenAI 暫時連不上 | 每次最多等 15 秒、重試 1 次，最壞約 1 分鐘，回 503 | 測試（test_ask_returns_503_when_ai_unavailable） |
| 轉向量 / 生成 | OpenAI 永久性錯誤 | 回 503「系統設定有誤」 | 測試（test_ask_returns_503_when_misconfigured）、☁ 待演練（本機用錯的 key 實測） |

## 5. 目前偵測不到的（留給主線 C）

系統能自己處理，但**沒有人會知道發生了**，要靠 C 的監控和告警：

- dead-letter queue 裡出現工單（代表有東西壞到自己處理不了）
- OpenAI 永久性錯誤（key 失效、沒錢了，所有新文件都會失敗）
- 處理失敗的比例突然升高

## 6. 已知、接受的限制

- **換密碼後，熱狀態的 Lambda 還在用舊密碼**：密碼只在冷啟動時讀一次。換密碼後要更新一次 Lambda（或等它自然冷啟動）。
- **對帳會把資料庫所有文件的檔案位置一次讀進記憶體**：文件數量到幾十萬份才會是問題，目前不處理。
