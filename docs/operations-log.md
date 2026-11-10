# 維運紀錄

系統上線後發生過的事：怎麼發現的、原因、怎麼處理、之後怎麼避免。

收到告警、或發現異狀時記一筆。格式：

```
## 日期：一句話描述
- 怎麼發現的：
- 影響：
- 原因：
- 處理：
- 之後怎麼避免：
```

告警的設定見 [decisions/015](decisions/015-告警與運作紀錄.md)，查問題的方法見 CloudWatch Logs Insights（`filter level != "INFO"`）。

## 回退用的版本紀錄

上線過、確認能用的版本。要退回時，Lambda 用 digest 指定（標籤可以被改指到別的 image，digest 不會），指令見 [deployment-commands.md](deployment-commands.md) 的「退回上一版」。

| 版本 | 上線 | Lambda image 標籤（commit） | Lambda image digest | 前端檔案 | 前端怎麼還原 |
|---|---|---|---|---|---|
| 重構前 | 2026-10-30（CI） | `49b6272c07dd7c7a131eae52feea4a0d30427ff6` | `sha256:69fe518589f99b4dc9d14c87b45f0412244285432ee4c638675209f059dc2115` | `index-D_9sjDA9.js`、`index-By4obAkl.css` | build commit `49b6272` 的 frontend（2026-11-10 驗過：build 出來的 JS 跟部署前的備份一模一樣，sha256 `bb619b47…`）；或用筆電上的備份 `後端練習/deploy-backups/2026-11-10-frontend-before-cloud-core/`（不進 git，裡面的 README.txt 有還原指令） |
| Cloud Core 手動部署 | 2026-11-10（手動） | `091102649a0f472534e97771f2c3cbcd09e3ffc8` | `sha256:b352ceb4a3068ba40d5773f8a59e2f01266680acb17d196845fef4abc145719f` | `index-Dc37YbBl.js`、`index-By4obAkl.css` | build commit `0911026` 的 frontend |

- ECR 沒有設 lifecycle policy：舊的 image 不會被自動刪掉，上面的 digest 都還在
- 前端的 bucket 沒有開版本控制：被 `s3 sync --delete` 換掉的檔案在 S3 上找不回來，只能重新 build 那個 commit，或用備份
- Vite 的檔名是照內容算的：同一個 commit、同一份 package-lock.json，在筆電、Docker、CI 上 build 出來的檔名都一樣，對得上檔名就是同一版

---

## 2026-11-10：Cloud Core（B1～B4）和可讀性整理（A2～A6）手動部署

- 怎麼發現的：不是故障，是計畫內的部署。程式在 `refactor/cloud-core` 分支，沒有 push 到 main，所以用「手動部署」的流程（docs/deployment-commands.md），不經過 GitHub Actions
- 影響：三個 Lambda 換成 commit `0911026` 的 image（digest `sha256:b352ceb4…`），前端換成新的 build。沒有 migration、沒有改 SQS、DLQ、EventBridge、IAM、Secrets
- 原因：—
- 處理（順序）：
  1. `aws logs test-metric-filter` 驗問答那條篩選條件（新舊兩種 `error_type` 都接受）的語法：AWS 接受，第 1、2 筆符合、第 3 筆不符合
  2. 只更新 API 的 `trace-app-openai-permanent` 篩選條件（其他篩選條件、告警、SNS、帳單上限都沒動）
  3. 從乾淨的 checkout build image（不含本機不進 git 的檔案）、推到 ECR、更新三個 Lambda
  4. worker 健康檢查 `{"health_check": true}` 回 `{"database": "ok"}`；經過 CloudFront 打 `/auth/me` 回 401；部署後沒有任何 ERROR 的 log
  5. 前端：先備份 bucket，再 `s3 sync --delete`、清 CloudFront 快取，網站拿到的是新的 JS
  6. 比對部署前後：SQS 觸發、EventBridge 排程、Lambda 的 timeout 和記憶體都沒變；DLQ 0 張；告警都是 OK
- 途中遇到的：這台電腦的網路斷斷續續（DNS 每二十多分鐘斷幾分鐘），在遠端 Neon 上跑的完整 pytest 兩次被打斷，改用本機 Docker 的 PostgreSQL（跟 CI 一樣）跑完 227 個；Docker build 下載套件逾時一次、推 image 時一層逾時一次，重試後成功
- 回退：三個 Lambda 指回重構前的版本，前端重新 build `49b6272` 或用備份（版本、digest、備份位置見上面的「回退用的版本紀錄」）
- 之後要注意：**正式環境現在跑的程式不在 main 上**。push 到 main 之前要先把 `refactor/cloud-core` 合併進去，不然 CI 會把舊版部署回來。前端還沒有人實際點擊驗證

## 2026-10-19：CI/CD 第一次部署被 AWS 拒絕

- 怎麼發現的：GitHub Actions 的部署步驟失敗，錯誤是 `Not authorized to perform sts:AssumeRoleWithWebIdentity`
- 影響：沒有。失敗在拿權限那一步，image 沒推、Lambda 沒改，雲端一直是上一個正常的版本
- 原因：GitHub 發的身分證明，格式是 `repo:gunter0128@帳號編號/traceable-research-assistant@專案編號:ref:refs/heads/main`，比網路上常見的寫法多了編號，跟 AWS 角色的信任條件對不上
- 處理：到 CloudTrail 查 `AssumeRoleWithWebIdentity` 的紀錄，從 `userIdentity` 拿到 GitHub 實際送來的值，照著改信任條件，重跑失敗的部分
- 之後怎麼避免：部署文件記下「不確定格式時，先讓它失敗一次、到 CloudTrail 查實際的值」

## 2026-10-17：更新 image 後，第一個請求等了 14～16 秒

- 怎麼發現的：剛建好 API 的 Lambda，第一次請求花了 14 秒；worker 的 log 裡 `processed` 只花 11 秒，`REPORT` 卻是 17.6 秒，前面有一行 `INIT_REPORT ... Status: timeout`
- 影響：每次發布新版本後的第一個使用者，要多等十幾秒
- 原因：新的 image 第一次啟動時，Lambda 要先從 ECR 下載整個 image，準備時間超過 10 秒的上限被中斷、重來一次
- 處理：CI/CD 部署完會自動「熱身」（主動叫醒 worker、打一次 API），讓 Lambda 先下載好，使用者就不會是第一個
- 之後怎麼避免：熱身寫在部署流程裡，每次都會做

## 2026-10-16：處理一份文件要 14 秒，一半的時間在等資料庫

- 怎麼發現的：剛加上結構化 log，看到「報平安」一次要 721 ms、取得租約要 4 秒
- 影響：上傳到處理完約 23 秒
- 原因：API 和 Lambda 在雪梨，資料庫在新加坡，每次存取來回 170 ms，而程式做每件事都要來回好幾趟
- 處理：AWS 全部搬到新加坡（[decisions/011](decisions/011-搬到新加坡.md)），每次存取降到 3 ms，上傳到處理完變成約 11.5 秒
- 之後怎麼避免：架構決策要考慮「哪一段距離會被放大」，用實際量測決定

## 2026-10-15：故障演練（上線前）

刻意製造故障，確認系統的反應跟設計一樣，詳細見 [failure-modes.md](failure-modes.md)：

- OpenAI 連不上：每回合間隔剛好 2 分鐘，8.6 分鐘後標失敗，修好後按重試 16 秒處理完
- worker 處理到一半被殺掉：5.3 分鐘後被判定中斷，按重試 20 秒處理完。第一次演練時發現文件會永遠卡住，因此加了心跳和租約（[decisions/008](decisions/008-心跳與租約.md)）
