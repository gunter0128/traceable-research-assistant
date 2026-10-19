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

---

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
