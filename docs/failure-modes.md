# 故障清單

系統每一步壞掉時會發生什麼事，以及證據。設計理由見 `decisions/`。

證據的標記：**測試** = 自動化測試（括號是測試名稱）、**雲端** = 在 AWS 上實際製造故障驗證過、**—** = 還沒有證據。

> A5（2026-10-15）：列清單時找到三個缺口（S3 錯誤沒分類、上傳時 S3 失敗回 500、兩個沒測試的情況），已補上。雲端演練 3 又發現一個：程式在「次數 +1」之前就當掉時，文件會永遠卡在處理中，改成加 15 分鐘的處理期限（decisions/007）。演練的過程和數字見最後一節。

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
| 送工單 | 記錄送出時間時資料庫失敗 | 紀錄停在處理中、沒送出工單；看上傳時間，15 分鐘後標「處理逾時，請按重試」 | 測試（test_sweep_expires_document_without_queued_at） |
| 送工單 | 工單其實送到了，但回應遺失 | 文件被標失敗，worker 收到工單時看到不是「處理中」就跳過；使用者按重試即可 | 接受：極少見，結果是多按一次重試 |

## 2. 文件處理（Lambda worker，背景執行）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 拿工單 | 文件已經被刪掉 | 跳過 | 測試（test_worker_skips_deleted_document） |
| 拿工單 | 重複的工單（文件已處理完） | 跳過，不會重複呼叫 OpenAI | 測試（test_worker_skips_duplicate_message） |
| 拿工單 | 資料庫連不上 | 連「次數 +1」都做不到；15 分鐘後標「處理逾時，請按重試」 | 測試（test_listing_expires_overdue_document）、雲端（演練 3 的同類情況） |
| 開始 | 已經處理 3 次 | 不處理，直接標失敗 | 測試（test_process_stops_after_max_attempts） |
| 下載 PDF | S3 暫時連不上 | 跟 OpenAI 一樣：2 分鐘後再一回合，最多 3 回合 | 測試（test_storage_temporary_error_defers_in_cloud、test_s3_errors_are_classified） |
| 下載 PDF | 檔案不見了 | 標失敗「找不到上傳的檔案，請刪除後重新上傳」，不重試 | 測試（test_missing_file_fails_with_clear_reason）；真的 S3 回的錯誤也確認會被分類成「檔案不存在」 |
| 下載 PDF | 沒權限讀檔案 | 標失敗「系統設定有誤」，不重試 | 測試（test_s3_errors_are_classified） |
| 解析 | PDF 壞掉 / 超過 300 頁 / 抓不到字 | 標失敗並寫明原因，不重試 | 測試（test_corrupt_pdf_has_reason 等三個） |
| 轉向量 | OpenAI 暫時連不上 | 秒級重試 4 次；不行就 2 分鐘後再一回合，最多 3 回合 | 測試（test_temporary_error_*）、雲端（演練 1：8.6 分鐘後標失敗，間隔都剛好 2 分鐘） |
| 轉向量 | OpenAI 永久性錯誤（key 無效、餘額不足） | 立刻標失敗「系統設定有誤」 | 測試（test_permanent_error_fails_immediately）、本機真的 OpenAI（演練 2） |
| 存 chunk | 中途任何錯誤 | rollback，一段都不留 | 測試（test_failed_processing_leaves_no_chunks） |
| 整個執行 | Lambda 被強制中斷（超時、記憶體不夠） | 沒 commit 的資料隨連線中斷自動丟棄；同一張工單 3 次後移到 dead-letter queue；15 分鐘後標「處理逾時」 | 雲端（演練 3：3 次後確實進 dead-letter queue），測試（test_sweep_expires_only_overdue_documents） |
| 整個執行 | 在「次數 +1」之前就被中斷 | 次數不會增加，次數上限擋不住；靠 15 分鐘期限標逾時 | 雲端（演練 3 發現的），測試（test_listing_expires_overdue_document） |
| 延後工單 | 沒有「延後」的權限 | 這次執行失敗，工單照預設 60 分鐘後重送（變慢，但不會遺失） | 雲端（演練 1 證明權限有效） |

## 3. 每小時掃描（Lambda sweeper）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 標逾時 | 交出去處理超過 15 分鐘還沒完成（沒人在看的文件） | 標「處理逾時，請按重試」，不重送 | 測試（test_sweep_expires_only_overdue_documents） |
| 對帳 | 孤兒檔案 | 放超過 1 天才刪，避免誤刪正在上傳的檔案 | 測試（test_orphan_cleanup_only_deletes_old_unreferenced_files）、☁ 待確認（2026-10-16 查測試用孤兒檔案有沒有被刪） |
| 對帳 | S3 列不出來 | 這次執行失敗，但標逾時已經先做完了；下個小時再試 | 接受 |

## 4. 問答（API，使用者在等）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 搜尋 | 只有失敗或處理中的文件 | 搜不到，回「文件中找不到相關資訊」 | 測試（test_search_similar_skips_unprocessed_documents） |
| 轉向量 / 生成 | OpenAI 暫時連不上 | 每次最多等 15 秒、重試 1 次，最壞約 1 分鐘，回 503 | 測試（test_ask_returns_503_when_ai_unavailable）、本機演練（32 秒回 503，第一步轉向量就失敗，沒走到生成答案） |
| 轉向量 / 生成 | OpenAI 永久性錯誤 | 回 503「系統設定有誤」 | 測試（test_ask_returns_503_when_misconfigured）、本機真的 OpenAI（演練 2：0.8 秒回 503，沒有白白重試） |

## 5. 目前偵測不到的（留給主線 C）

系統能自己處理，但**沒有人會知道發生了**，要靠 C 的監控和告警：

- dead-letter queue 裡出現工單（代表有東西壞到自己處理不了）
- OpenAI 永久性錯誤（key 失效、沒錢了，所有新文件都會失敗）
- 處理失敗的比例突然升高

## 6. 已知、接受的限制

- **換密碼後，熱狀態的 Lambda 還在用舊密碼**：密碼只在冷啟動時讀一次。換密碼後要更新一次 Lambda（或等它自然冷啟動）。
- **對帳會把資料庫所有文件的檔案位置一次讀進記憶體**：文件數量到幾十萬份才會是問題，目前不處理。

## 7. 演練紀錄（2026-10-15）

| 演練 | 怎麼製造故障 | 觀察到的 | 結論 |
|---|---|---|---|
| 1. OpenAI 連不上（雲端） | worker 的環境變數 `OPENAI_BASE_URL` 改成不存在的位址 `http://10.255.255.1/v1`，不改程式碼 | 每回合約 90 秒（秒級重試）；第 1、2 回合結束後工單都剛好隔 2 分鐘回來；第 3 回合標失敗，上傳後 8.6 分鐘；移除故障後按重試，16 秒處理完 | 「延後工單」的權限第一次被真正用到，有效；設計的 10 分鐘上限成立。中途我把估算改成 12 分鐘是錯的，實測才是準的 |
| 2. OpenAI key 錯誤（本機） | 本機用假的 key 啟動 API | 上傳的文件標「系統設定有誤」；問答 0.8 秒回 503 | 永久性錯誤分類正確，沒有白白重試 |
| 問答：OpenAI 連不上（本機） | 本機用不存在的 OpenAI 位址啟動 API | 32 秒回 503「AI 服務暫時無法使用」 | 第一步轉向量就失敗（15 秒 × 2 次），沒走到生成答案，所以不到最壞的 1 分鐘 |
| 3. Lambda 被強制中斷（雲端） | worker 逾時暫時改 1 秒、主佇列可見性逾時暫時改 60 秒 | 每次都剛好在 1000 毫秒被中斷；3 次後工單移到 dead-letter queue；**文件的處理次數一直是 0** | dead-letter queue 有效。但發現程式在「次數 +1」之前就當掉時，文件會永遠卡在處理中 → 加 15 分鐘的處理期限（decisions/007） |
