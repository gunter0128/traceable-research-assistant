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
| 確認檔案 | S3 暫時連不上 | 回 503「儲存服務暫時無法使用，請稍後再試」，不建紀錄 | 測試（test_upload_returns_503_when_storage_unavailable） |
| 建紀錄 | 資料庫寫入失敗 | 把剛傳上去的 S3 檔案刪掉，回 500 | 測試（test_upload_removes_file_when_record_fails） |
| 建紀錄 | 連刪檔案也失敗 | 留下孤兒檔案，每小時對帳清掉 | 測試（test_orphan_cleanup_only_deletes_old_unreferenced_files） |
| 送工單 | SQS 送不出去 | 標失敗「排入處理佇列失敗，可以重試」 | 測試（test_upload_marks_failed_when_send_fails） |
| 送工單 | 記錄送出時間時資料庫失敗 | 紀錄停在處理中、沒送出工單；看上傳時間，15 分鐘後標「處理逾時，請按重試」 | 測試（test_sweep_expires_document_without_queued_at） |
| 送工單 | 工單其實送到了，但回應遺失 | 文件被標失敗，worker 收到工單時看到不是「處理中」就跳過；使用者按重試即可 | 接受：極少見，結果是多按一次重試 |

## 2. 文件處理（Lambda worker，背景執行）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 拿工單 | 文件已經被刪掉 | 跳過 | 測試（test_worker_skips_deleted_document） |
| 拿工單 | 重複的工單（文件已處理完，或另一個 Lambda 正在處理） | 跳過，不會重複呼叫 OpenAI | 測試（test_worker_skips_duplicate_message、test_duplicate_message_while_processing_is_skipped：兩條執行緒真的同時跑） |
| 拿工單 | 資料庫連不上 | 連租約都拿不到，文件還在排隊中；15 分鐘後標「處理逾時，請按重試」 | 測試（test_listing_expires_overdue_document） |
| 拿工單 | 兩個 Lambda 同時拿到同一份文件的工單 | 只有一個搶得到租約，另一個直接跳過 | 測試（test_claim_is_exclusive、test_concurrent_claims_only_one_succeeds：兩條資料庫連線同時搶） |
| 拿工單 | Lambda 處理到一半死掉，SQS 約 1 小時後重送同一張工單 | 文件還在處理中，搶不到租約：不接手、不重做、次數不增加；10 分鐘沒報平安後標「處理中斷」，使用者按重試（decisions/046。以前會接手，再靠「處理 3 次就不再處理」的次數上限擋住一直讓程式當掉的文件；不接手之後次數最多是 1，上限拿掉了） | 測試（test_redelivered_message_does_not_reprocess_interrupted_document、test_only_uploaded_document_can_be_claimed、test_interrupted_document_is_not_taken_over_but_can_be_retried、test_attempt_count_does_not_block_processing） |
| 下載 PDF | S3 暫時連不上 | 跟 OpenAI 一樣：標失敗「儲存服務暫時無法使用，請稍後按重試」，使用者按重試（decisions/046） | 測試（test_storage_temporary_error_fails_in_cloud、test_s3_errors_are_classified） |
| 下載 PDF | 檔案不見了 | 標失敗「找不到上傳的檔案，請刪除後重新上傳」，不重試 | 測試（test_missing_file_fails_with_clear_reason）；真的 S3 回的錯誤也確認會被分類成「檔案不存在」 |
| 下載 PDF | 沒權限讀檔案 | 標失敗「系統設定有誤」，不重試 | 測試（test_s3_errors_are_classified） |
| 解析 | PDF 壞掉 / 超過 300 頁 / 抓不到字 | 標失敗並寫明原因，不重試 | 測試（test_corrupt_pdf_has_reason 等三個） |
| 轉向量 | OpenAI 暫時連不上 | OpenAI SDK 內建的重試，一共試 3 次（間隔 0.5、1 秒）；還是不行就標失敗「AI 服務暫時無法使用，請稍後按重試」，工單不延後（decisions/046；B3-1 之前是自己寫的重試、最多 5 次；更早是 2 分鐘後再一回合，最多 3 回合）。chunks 最後才寫，所以一段都不會留；前面幾批已經花掉的用量照樣有紀錄。每次重試 SDK 會寫一筆 INFO「Retrying request in …」 | 測試（test_temporary_error_*、test_worker_finishes_normally_on_temporary_error、test_temporary_failure_can_be_retried、test_failed_second_batch_keeps_usage_and_saves_no_chunks；用真的 SDK 測：test_temporary_errors_are_retried、test_gives_up_after_retries_as_temporary_error、test_sdk_retry_is_logged_as_json）；雲端的演練 1 是改之前的做法 |
| 轉向量 | OpenAI 說「請等 N 秒」 | 120 秒以內 SDK 照做；超過 120 秒 SDK 不重試，直接標失敗「AI 服務暫時無法使用」（B3-1 之前自己寫的重試最多等 60 秒） | 測試（test_follows_retry_after、test_does_not_wait_longer_than_sdk_limit） |
| 轉向量 | OpenAI 永久性錯誤（key 無效、餘額不足） | 標失敗「系統設定有誤」，觸發告警。key 無效、格式錯誤不重試；餘額不足跟速率限制都是 429，SDK 分不出來，會先重試 2 次（多等約 1.5 秒）才標失敗 | 測試（test_permanent_error_fails_immediately；用真的 SDK 測：test_permanent_error_is_not_retried、test_insufficient_quota_ends_as_permanent_error）、本機真的 OpenAI（演練 2，B3-1 之前的做法） |
| 轉向量 | 記用量時資料庫出錯 | 這一批的錢花了但沒記到（每日上限會少算這一批）；不再轉下一批（不再花錢），標失敗「處理時發生錯誤」，寫 ERROR 和錯誤堆疊（觸發告警） | 測試（test_usage_record_failure_stops_embedding_and_logs_error） |
| 轉向量 | 轉得很慢 | 每轉完一批報一次平安；一批加上 SDK 所有的重試估算約 5.5 分鐘（3 次 × 30 秒 + 2 次等待 × 120 秒），比 10 分鐘的期限短（decisions/046）。這是估算不是保證：30 秒是連線和每一次讀取的上限，不是整個請求的總時間，回應一直慢慢傳的話一批還是可能更久、被誤判成中斷（存結果會被擋下來，資料不會錯）。部署後在 AWS 上量 | 測試（test_heartbeat_timeout_covers_one_batch_with_all_retries） |
| 存 chunk | 中途任何錯誤 | rollback，一段都不留 | 測試（test_failed_processing_leaves_no_chunks） |
| 存 chunk | 最後一次報平安之後才被判定中斷 | 存結果時有條件的 UPDATE 改不到，整批放棄，一段都不留 | 測試（test_lease_lost_after_last_heartbeat_saves_no_chunks） |
| 報平安 | 資料庫出錯 | 標失敗，不存 chunks | 測試（test_heartbeat_error_fails_document_without_saving_chunks） |
| 整個執行 | Lambda 處理到一半被強制中斷（超時、記憶體不夠） | 沒 commit 的資料隨連線中斷自動丟棄；停止報平安，10 分鐘後標「處理中斷，請按重試」（decisions/008、046） | 測試（test_listing_marks_silent_document_interrupted）；雲端的演練 3 重做時是 5 分鐘的期限（上傳後 5.3 分鐘標中斷） |
| 整個執行 | 在取得租約之前就被中斷 | 文件還在排隊中，15 分鐘後標「處理逾時」 | 雲端（演練 3 發現的，decisions/007），測試（test_listing_expires_overdue_document） |
| 整個執行 | 以前取消的文件（取消功能 decisions/046 拿掉了，舊資料還在） | 照常顯示「已取消」；又收到工單時搶不到租約、不處理；可以按重試、刪除。開著舊畫面的人按到取消，API 回 404，文件不會被改到 | 測試（test_old_cancelled_document_is_still_listed、test_old_cancelled_document_is_skipped_by_worker、test_old_cancelled_document_can_be_retried、test_cancel_endpoint_is_removed） |
| 整個執行 | 舊的工單在使用者按重試之後才出現（例如上一輪的 worker 當掉，SQS 1 小時後重送；或工單其實送到了，但回應遺失） | 跟重試送出的新工單是同一份文件：先到的搶到租約處理，後到的看到不是排隊中就跳過，不會處理兩次、不會存出兩份。限制：可能是舊工單搶先處理，log 裡帶的是舊請求的編號；先到的那次如果失敗，後到的也會跳過，要再按一次重試 | 測試（test_worker_skips_duplicate_message、test_duplicate_message_while_processing_is_skipped、test_only_uploaded_document_can_be_claimed） |
| 整個執行 | 其實沒死只是很慢，被判定中斷後使用者按了重試 | 舊的 Lambda 報平安或存結果時發現租約不是自己的，放棄這次處理，不會存出兩份 | 測試（test_slow_worker_gives_up_after_retry_started_new_worker、test_heartbeat_rejected_after_interrupted、test_finish_rejected_with_wrong_token） |
| 整個執行 | 處理到一半，使用者把文件刪掉 | 租約跟著文件一起沒了：報平安、存結果、標失敗都改不到任何東西，停下來；worker 正常結束（不寫 ERROR、工單不重送），本機模式上傳的請求回 404 | 測試（test_worker_stops_quietly_when_document_deleted_while_embedding、test_document_deleted_just_before_saving、test_document_deleted_then_processing_fails、test_inline_upload_returns_404_when_document_deleted_while_processing） |
| 整個執行 | 使用者連按兩次重試 | 改回排隊中是有條件的更新（只有失敗、已取消的改得到）：第二個請求改不到、回 409，不會把已經在處理的文件又改回排隊中 | 測試（test_retry_race_does_not_reset_document_being_processed） |

## 3. 每小時掃描（Lambda sweeper）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 標中斷 / 逾時 | 沒人在看的文件：處理中 10 分鐘沒報平安、排隊中 15 分鐘沒人處理 | 標「處理中斷」/「處理逾時，請按重試」，不重送 | 測試（test_sweep_marks_silent_document_interrupted、test_sweep_expires_only_overdue_documents） |
| 對帳 | 孤兒檔案 | 放超過 1 天才刪，避免誤刪正在上傳的檔案 | 測試（test_orphan_cleanup_only_deletes_old_unreferenced_files）、☁ 待確認（2026-10-16 查測試用孤兒檔案有沒有被刪） |
| 對帳 | S3 列不出來 | 這次執行失敗，但標逾時已經先做完了；下個小時再試 | 接受 |

## 4. 問答（API，使用者在等）

| 哪一步 | 故障 | 系統的反應 | 證據 |
|---|---|---|---|
| 搜尋 | 只有失敗或處理中的文件 | 搜不到，回「文件中找不到相關資訊」 | 測試（test_search_similar_skips_unprocessed_documents） |
| 轉向量 / 生成 | OpenAI 暫時連不上、超時 | 只送一次、不重試（使用者在等，decisions/046 B3-2），回 503「AI 服務暫時無法使用」。轉向量最多等 15 秒；生成最多等 15 秒（地端 45 秒），而且不超過整個問答剩下的時間（50 秒的預算）。轉向量成功、生成失敗的話，轉向量的用量照樣記（B3-2 之前每次重試 1 次） | 測試（test_ask_returns_503_when_ai_unavailable；用真的 SDK 測：test_qa_calls_are_not_retried、test_ask_answer_failure_returns_503_and_keeps_embedding_usage）、本機演練（32 秒回 503，B3-2 之前的做法） |
| 轉向量 / 生成 | OpenAI 說「請等 N 秒」 | 不等、不重試，直接回 503（B3-2 之前是 5 秒以內照做；更早套件會照做最多 120 秒） | 測試（test_qa_calls_are_not_retried 的 429-retry-after） |
| 生成 | 前面花太久（例如資料庫很慢），整個問答的預算剩不到 1 秒 | 不送生成答案的請求，回 503；轉向量的用量照樣記 | 測試（test_budget_used_up_skips_answer、test_answer_timeout_is_capped_by_remaining_budget） |
| 搜尋 | 轉向量成功之後，搜尋出了預料之外的錯誤 | 回 500；轉向量的用量照樣記（拿到用量就先放進清單，最後在 finally 寫入。改之前靠 on_usage callback 也是這樣，B3-2 改成回傳值後行為不變） | 測試（test_search_failure_after_embedding_still_records_usage） |
| 任何 API | 資料庫連不上 | 回 503「服務暫時無法使用，請稍後再試」（以前是 500） | — |
| 轉向量 / 生成 | OpenAI 永久性錯誤 | 回 503「系統設定有誤」，觸發告警（qa_failed、permanent）；只送一次，餘額不足也不會先白試 | 測試（test_ask_returns_503_when_misconfigured、test_qa_permanent_errors）、本機真的 OpenAI（演練 2：0.8 秒回 503，沒有白白重試） |

## 5. 需要人處理時，怎麼知道

系統自己處理不了的，寄告警 email（decisions/015，`deploy/setup_monitoring.py`）：

- dead-letter queue 裡出現工單（代表有東西壞到自己處理不了）
- OpenAI 永久性錯誤（key 失效、沒錢了，所有新文件都會失敗）：處理文件、問答兩邊的 log 都比對 `error_type = "OpenAIPermanentError"`；tests/test_alarms.py 確認金鑰失效、餘額不足會符合、暫時性的錯誤不會
- （還沒有）S3 的永久性錯誤（沒權限、bucket 設錯）：見 [refactor/FOLLOW_UPS.md](refactor/FOLLOW_UPS.md)
- 程式錯誤的 log（預料之外的錯誤）
- 全系統當天的用量到上限
- 每小時的 sweeper 沒有跑

## 6. 已知、接受的限制

- **換密碼後，熱狀態的 Lambda 還在用舊密碼**：密碼只在冷啟動時讀一次。換密碼後要更新一次 Lambda（或等它自然冷啟動）。
- **對帳會把資料庫所有文件的檔案位置一次讀進記憶體**：文件數量到幾十萬份才會是問題，目前不處理。

## 7. 演練紀錄（2026-10-15）

> 這些演練是 [decisions/046](decisions/046-簡化上雲後的可靠性機制.md) 之前做的，當時還有自動延後重試（每回合、2 分鐘後再送）、5 分鐘的心跳期限和接手。觀察到的數字是當時的紀錄，照舊保留；現在的行為看上面的表。新的機制部署之後要重做的演練（例如演練 3），列在 [refactor/FOLLOW_UPS.md](refactor/FOLLOW_UPS.md)。

| 演練 | 怎麼製造故障 | 觀察到的 | 結論 |
|---|---|---|---|
| 1. OpenAI 連不上（雲端） | worker 的環境變數 `OPENAI_BASE_URL` 改成不存在的位址 `http://10.255.255.1/v1`，不改程式碼 | 每回合約 90 秒（秒級重試）；第 1、2 回合結束後工單都剛好隔 2 分鐘回來；第 3 回合標失敗，上傳後 8.6 分鐘；移除故障後按重試，16 秒處理完 | 「延後工單」的權限第一次被真正用到，有效；設計的 10 分鐘上限成立。中途我把估算改成 12 分鐘是錯的，實測才是準的 |
| 2. OpenAI key 錯誤（本機） | 本機用假的 key 啟動 API | 上傳的文件標「系統設定有誤」；問答 0.8 秒回 503 | 永久性錯誤分類正確，沒有白白重試 |
| 問答：OpenAI 連不上（本機） | 本機用不存在的 OpenAI 位址啟動 API | 32 秒回 503「AI 服務暫時無法使用」 | 第一步轉向量就失敗（15 秒 × 2 次），沒走到生成答案，所以不到最壞的 1 分鐘 |
| 3. Lambda 被強制中斷（雲端） | worker 逾時暫時改 1 秒、主佇列可見性逾時暫時改 60 秒 | 每次都剛好在 1000 毫秒被中斷；3 次後工單移到 dead-letter queue；**文件的處理次數一直是 0** | dead-letter queue 有效。但發現程式在「次數 +1」之前就當掉時，文件會永遠卡在處理中 → 加 15 分鐘的處理期限（decisions/007） |
| 3 重做（雲端，decisions/008 之後） | worker 逾時暫時改 20 秒，加上 `OPENAI_BASE_URL` 改成不存在的位址，讓它拿到租約、開始處理之後才被中斷 | 上傳 17 秒後取得租約（處理中 0 / 1）；20 秒時被強制中斷；之後「距上次報平安」一路增加，310 秒時標「處理中斷，請按重試」，上傳後 5.3 分鐘；恢復設定後按重試，20 秒處理完成 | 心跳有效：從「乾等 15 分鐘」縮短到約 5 分鐘，而且是看到程式停止報平安才判定，不是猜的。API 這次跑的確實是新版 image（強制重新部署的規則有效） |
