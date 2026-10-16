# 部署指令小抄

V2(部署到 AWS)過程中用到的指令,整理起來備查。指令不像程式碼,下過之後不會留在專案裡,容易忘記在幹嘛、哪些要重複做、哪些只做一次。

## 開始之前:這台電腦需要先裝好的工具

換一台新電腦接手這個專案,git clone 下來之後,光有程式碼還不夠,這幾個工具都要另外裝,而且**都是裝給作業系統用的,不會跟著 git 走**:

| 工具 | 用途 | 下載 |
|---|---|---|
| Python 3.11 + venv | 跑後端(`requirements.txt`) | 通常電腦已有,沒有就去 [python.org](https://www.python.org/) |
| Docker Desktop | build/跑容器、之後部署用的 image | [docker.com](https://www.docker.com/products/docker-desktop/) |
| AWS CLI v2 | 操作 AWS(ECR、ECS、Secrets Manager...) | [AWS 官方安裝頁](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),裝完要 `aws configure` 用 `chun-admin` 的金鑰登入 |
| Node.js(LTS) | 跑前端(`frontend/`),裝的時候會順便裝好 `npm` | [nodejs.org](https://nodejs.org),選 **Windows 安裝程式(.msi)**,目前用的是 v24.21.0 |

**每裝完一個,一定要關掉目前開著的終端機、重新開一個,再去跑對應的 `--version` 指令確認。** 這專案已經中過好幾次招:裝好之後沒換新終端機,`docker`/`aws`/`node` 全部都抓不到,因為 PATH(系統去哪裡找這些指令)是終端機一開始啟動時就讀好的,裝新軟體不會讓已經開著的終端機重新讀一次。

## 先搞懂語法結構

這份裡大部分指令都是同一種結構:

```
工具名稱 子指令 參數...
```

例如 `docker tag ...`、`docker push ...`、`aws ecr create-repository ...`——`docker`/`aws` 是工具本身,`tag`/`push`/`ecr create-repository` 是子指令(告訴工具這次要做哪種動作),後面才是參數。這個結構不是 Docker、AWS 特有的,`git commit -m "..."` 也是同一套邏輯(`git` 工具、`commit` 子指令)。認出這個結構,以後看到新的 CLI 工具也比較看得懂在幹嘛。

---

## Docker:本機 build + 測試

### 建立 image

```bash
docker build --platform linux/amd64 --provenance=false -t traceable-research-assistant .
```
照 [Dockerfile](../Dockerfile) 的步驟組出一個 image。

- `--platform linux/amd64`——明確指定 build 給 Intel/AMD 架構的 Linux（Lambda 預設的架構）。這台電腦本來就是，不寫也一樣，但換到 Apple 晶片的 Mac 就會 build 出 Lambda 跑不了的 image
- `--provenance=false`——**Lambda 一定要加**。新版 Docker 預設會附加 provenance 資訊，image 會變成 Lambda 不接受的格式（ECR 上看到 `image.index` 就是中招了，正確的是 `image.manifest`）。Fargate 不在意這件事`-t` 後面是取的名字,沒指定標籤(`:版本`)時,Docker 自動補 `:latest`。

**`:latest` 不是「保證最新版」的機制,只是沒指定標籤時的預設名字**。每次沒指定標籤重新 build,新的 image 會把這個名字蓋過去,感覺上「latest 一直是最新的」,但這是因為你一直用同一個預設名字,不是 Docker 在幫你追蹤版本。

**什麼時候要重做**:改了程式碼、想測新版本的時候。

### 啟動容器測試

```bash
docker run -d -p 8000:8000 --env-file .env --name trace-test traceable-research-assistant
```
- `-d`——背景執行(detached)
- `-p 8000:8000`——port mapping,格式是 `本機埠:容器埠`,沒有這個外面連不進容器
- `--env-file .env`——把 `.env` 裡每一行當環境變數注入容器（**`.env` 格式要是 `KEY=值`，等號兩邊不能有空格，Docker 的解析器比 Python 那邊嚴格，空格會被當成值的一部分**）
- `--name trace-test`——幫容器取名，方便之後用 `docker logs trace-test`、`docker rm trace-test` 指名操作

### 檢查、收尾

```bash
docker ps              # 看目前「正在跑」的容器
docker ps -a            # 連「已經停止」的也列出來（logs 要看已停止的容器，用這個才找得到）
docker logs trace-test  # 看這個容器印出來的東西（程式的 print、錯誤訊息都在這）
docker rm -f trace-test # 強制刪掉這個容器（-f 代表不管它是不是還在跑，直接刪）
```

---

## AWS CLI:連上帳號

```powershell
aws configure
```
互動式輸入 Access Key ID、Secret Access Key、預設區域、輸出格式。**這組金鑰要用 `chun-admin`（自己的管理帳號），不是 `trace-app-s3`（那組權限被鎖死只能碰 S3，做不了 ECR 這些事）**。

```bash
aws sts get-caller-identity
```
只問「我現在是誰」,不會建立或修改任何東西,純驗證身份用。

---

## ECR:image 的雲端倉庫

### 建倉庫（只做一次）

```bash
aws ecr create-repository --repository-name traceable-research-assistant --region ap-southeast-2
```
建立之後會一直存在，**不用每次推 image 都重建**。

### 登入（有效期 12 小時，過期要重做）

```bash
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com
```
- `aws ecr get-login-password`——跟 AWS 要一組臨時通行證（12 小時後失效）
- `|`——管線，把左邊指令的輸出，直接餵給右邊指令當輸入
- `docker login --username AWS --password-stdin <網址>`——用這組通行證登入 ECR。`--username AWS` 的 `AWS` 是**固定寫死的字串**，不是你的帳號名稱；`--password-stdin` 代表密碼從管線接，不用手動打

**12 小時內要推好幾次，只要登入一次；隔久了再推，要重新跑這行拿新的通行證。**

**在 PowerShell（Windows PowerShell 5.1）上，上面那行會失敗**，出現 `400 Bad Request`：PowerShell 5.1 用 `|` 把一個程式的輸出傳給另一個程式時，會在密碼尾端多加換行字元，ECR 認不得。改成先存進變數再傳：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REPO = "$ACCOUNT.dkr.ecr.ap-southeast-2.amazonaws.com/traceable-research-assistant"
$PASSWORD = aws ecr get-login-password --region ap-southeast-2
docker login --username AWS --password $PASSWORD "$ACCOUNT.dkr.ecr.ap-southeast-2.amazonaws.com"
```
- `aws sts get-caller-identity`——問 AWS「我現在用哪個帳號登入」，只取帳號 ID，就不用手動貼那串數字
- 會出現「在指令裡寫密碼不安全」的警告，可以忽略：這是 12 小時後就失效的臨時密碼
- 這三個變數只存在目前這個終端機視窗，關掉重開要重跑
- 有了 `$REPO`，下面的 tag / push 可以寫成 `docker tag traceable-research-assistant:latest "${REPO}:latest"`、`docker push "${REPO}:latest"`（大括號是告訴 PowerShell 變數名稱到哪裡結束）

**指令裡的 `<task-id>`、`<stream-name>` 這種寫法是佔位符號，要換成真的值，不能照打**。PowerShell 會把 `<` 當成特殊符號，直接報 `The '<' operator is reserved for future use`。

### Tag + Push（每次要發布新版本都要做）

```bash
docker tag traceable-research-assistant:latest 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com/traceable-research-assistant:latest
docker push 416113247224.dkr.ecr.ap-southeast-2.amazonaws.com/traceable-research-assistant:latest
```

ECR 位址的結構(跟 GitHub 網址是同一種邏輯):
```
416113247224.dkr.ecr.ap-southeast-2.amazonaws.com / traceable-research-assistant : latest
└──────────伺服器網址（你帳號+區域專屬）──────────┘   └──倉庫名稱（對應 create-repository 時取的名字）──┘  └標籤┘
```

- **`docker tag`**——幫本機已經存在的 image，多貼一個新名字（這個新名字裡面包含 ECR 的位址），本體還是同一份 image，不會複製出第二份
- **`docker push`**——讀這個新名字裡的位址，把對應的 image 內容上傳過去。**`push` 本身不能指定「要送去哪」，完全靠讀 image 的名字來判斷，所以一定要先 `tag` 才能 `push`**

跟 `git add → commit → push` 一樣，**這是以後每次要發布新版本都要重複的日常流程**，不是一次性動作。

### 確認真的推上去了

```bash
aws ecr describe-images --repository-name traceable-research-assistant --region ap-southeast-2
```
列出這個倉庫裡現有的 image，`imageStatus: "ACTIVE"` 代表確實存在且可用。

---

## Secrets Manager：密鑰放哪

```bash
aws secretsmanager create-secret --name trace-app/database-url --secret-string "值" --region ap-southeast-2
aws secretsmanager update-secret --secret-id trace-app/database-url --secret-string "新值" --region ap-southeast-2
```
`create-secret` 第一次建立；改內容用 `update-secret`，不用整個刪掉重建（刪除預設有 30 天緩衝期，要立刻重建同名的會卡住，加 `--force-delete-without-recovery` 才會真的馬上刪）。

已建立的三個：`trace-app/database-url`、`trace-app/secret-key`、`trace-app/openai-api-key`（都在 `ap-southeast-2`）。

---

## 兩個 IAM Role（透過主控台建立，不是 CLI）

**`trace-app-execution-role`**——`arn:aws:iam::416113247224:role/trace-app-execution-role`
負責「啟動容器前」的事：抓 image、讀密鑰、送 log。掛了兩份政策：
- `AmazonECSTaskExecutionRolePolicy`（AWS 現成的，抓 image + 送 log）
- `SecretsAccessForTraceApp`（內嵌政策，讀上面那三個密鑰）

**`trace-app-task-role`**——`arn:aws:iam::416113247224:role/trace-app-task-role`
負責「容器真正跑起來之後」程式碼能做的事：碰 S3（取代本機開發用的 `trace-app-s3` 金鑰）。掛了一份政策：
- `S3AccessForTraceAppTaskRole`（內嵌政策，內容跟 `trace-app-s3` 一樣，限定只能碰這一個 bucket）

兩個 Role 的信任政策都一樣：只有 ECS 的任務服務（`ecs-tasks.amazonaws.com`）可以借用。這兩個 ARN，寫 Task Definition 時會用到。

---

## Task Definition：容器的「食譜」

寫在 [`deploy/task-definition.json`](../deploy/task-definition.json)，記錄「這個任務要用哪個 image、吃多少 CPU/記憶體、兩個 Role 分別是誰、環境變數跟密鑰從哪裡來、log 往哪裡送」。這份 JSON 可以直接在主控台的空白範本上填，也可以存成檔案用 CLI 註冊：

```bash
aws ecs register-task-definition --cli-input-json file://deploy/task-definition.json --region ap-southeast-2
```

每次註冊都會產生新的 revision（`trace-app:1`、`trace-app:2`...），舊的 revision 不會被覆蓋或刪除，只是不再是「最新」。**改了 Task Definition（比方說要用新 image、改環境變數）之後，要重新註冊產生新 revision，Service 才有新版本可以切換過去**——光改這份 JSON 檔案本身，不會自動影響正在跑的 Service。

```bash
aws ecs describe-task-definition --task-definition trace-app --region ap-southeast-2
```
只讀，確認目前最新 revision 的內容。

---

## Security Group：防火牆規則

透過主控台建立，叫 `trace-app-sg`。開放 inbound（外面連進來）TCP 8000 port，來源設成 `0.0.0.0/0`（任何地方都能連，因為這是公開 API，沒有另外架 VPN/內網）。outbound（容器對外連，比方說連 Neon 資料庫、呼叫 OpenAI API）預設全部放行，不用額外設定。

---

## ECS Cluster：組織容器的「資料夾」

透過主控台建立，叫 `trace-app-cluster`，選 Fargate（無伺服器，不用自己管底層機器）。Cluster 本身不跑任何東西，只是把相關的 Service/Task 歸在一起管理，方便之後在主控台找。

```bash
aws ecs describe-clusters --clusters trace-app-cluster --region ap-southeast-2
```

---

## ECS Service：讓任務「跑起來並保持活著」

透過主控台建立，叫 `trace-app-service`。指向 `trace-app-cluster`、用最新的 `trace-app` Task Definition、Desired tasks 設 1、網路選預設 VPC 的三個子網路 + `trace-app-sg`、**公有 IP 設成啟用**（不然外面連不進去）。

### 確認目前狀態(只讀)

```bash
aws ecs describe-services --cluster trace-app-cluster --services trace-app-service --region ap-southeast-2
```
看 `runningCount`（目前真的在跑幾個）、`desiredCount`（希望跑幾個）、`deployments`（部署歷史跟狀態）。

```bash
aws ecs describe-tasks --cluster trace-app-cluster --tasks <task-id> --region ap-southeast-2
```
看單一任務的詳細狀態，包括失敗時的 `stoppedReason`、`exitCode`——診斷崩潰問題的第一步。

### 查 log(容器印出來的東西，等同雲端版的 `docker logs`)

```bash
export MSYS_NO_PATHCONV=1   # Git Bash 專用：避免把 /ecs/trace-app 這種字串誤判成本機路徑
aws logs describe-log-streams --log-group-name /ecs/trace-app --region ap-southeast-2
aws logs get-log-events --log-group-name /ecs/trace-app --log-stream-name <stream-name> --region ap-southeast-2
```

### 套用新版本(改了程式碼、重新 build+push+register 之後)

```bash
aws ecs update-service --cluster trace-app-cluster --service trace-app-service --force-new-deployment --region ap-southeast-2
```
**`--force-new-deployment` 的用途**：就算 Task Definition 的 revision 號碼沒變（比方說只是用了同一個 `:latest` image tag、但 image 內容其實換新了），這個指令會強制 Service 不管快取，直接拉一份新的任務起來、健康後把舊的關掉。沒有這個指令，Service 會覺得「設定沒變啊」而繼續用舊的容器跑。

改了 `deploy/task-definition.json`（例如加環境變數）的話，要先 `register-task-definition` 登記新版本，再用 `--task-definition trace-app` 讓 Service 改用最新版：

```bash
aws ecs update-service --cluster trace-app-cluster --service trace-app-service --task-definition trace-app --desired-count 1 --force-new-deployment --region ap-southeast-2
```
Task Definition 用指令從檔案登記，不在主控台改：檔案是唯一的來源，主控台改了檔案不會跟著變。

跑完之後，用 `describe-services`/`describe-tasks` 確認新任務的 `runningCount` 變成 1、沒有立刻崩潰退出，再去 ECS 主控台的任務頁面抓新的公有 IP 測試(**每次任務重啟，公有 IP 會換**，不是固定網址)。

### 不用的時候記得關掉(避免持續計費)

```bash
aws ecs update-service --cluster trace-app-cluster --service trace-app-service --desired-count 0 --region ap-southeast-2
```
Fargate 是「任務在跑就按秒計費」，跟 OpenAI 那種按用量計費不一樣。測試完一段時間，把 `desired-count` 調成 0，之後要測再調回 1 即可，不用整個刪掉 Service。

---

## 背景處理：SQS + Lambda（A2，2026-10-15）

上傳後不在 API 裡處理文件，改成送工單到 SQS，由 Lambda 上的 worker 處理。為什麼這樣設計見 [decisions/003](decisions/003-背景處理用-sqs-加-lambda.md)、[decisions/004](decisions/004-lambda-的部署方式.md)。這裡只記建了哪些東西、怎麼建的、之後要怎麼維護。

```
API（Fargate）──送工單──▶ trace-app-documents-queue ──失敗 3 次──▶ trace-app-documents-dlq
                                  │
                                  ▼ 有工單就叫醒（每次 1 張、最多同時 2 個）
                         Lambda：trace-app-worker（handle_process_message）

EventBridge：trace-app-sweeper-hourly（每小時）──▶ Lambda：trace-app-sweeper（handle_sweep）
```

### SQS 佇列（只做一次）

設定寫在 `deploy/` 的 JSON 檔，PowerShell 直接把 JSON 寫在指令裡引號容易壞掉，用檔案比較穩。**dlq 要先建**，因為主佇列的設定會指向它：

```powershell
aws sqs create-queue --queue-name trace-app-documents-dlq --attributes file://deploy/sqs-dlq-attributes.json --region ap-southeast-2
aws sqs create-queue --queue-name trace-app-documents-queue --attributes file://deploy/sqs-queue-attributes.json --region ap-southeast-2
```
- 主佇列 visibility timeout 3600 秒：Lambda 拿走工單後，工單隱形這麼久。**一定要比 Lambda 的逾時（600 秒）長**，不然 Lambda 還在處理，工單就重新出現被處理第二次
- 同一張工單被拿 3 次都沒完成 → 移到 dlq，保留 14 天

### Lambda 的 IAM Role（主控台建立，只做一次）

`trace-app-worker-role`，兩個 Lambda 共用。IAM → Roles → Create role → AWS service / Lambda → 勾 `AWSLambdaBasicExecutionRole`（寫 log）→ 建好後再加一個內嵌政策，內容是 [deploy/lambda-permissions-policy.json](../deploy/lambda-permissions-policy.json)：只能用這一個佇列、只能讀 bucket 的 `documents/`、只能讀 `trace-app/` 開頭的密碼。

API 的 `trace-app-task-role` 另外加了內嵌政策 [deploy/api-task-sqs-policy.json](../deploy/api-task-sqs-policy.json)：只能送訊息到主佇列。

**在主控台改了權限，也要同步改 `deploy/` 裡的檔案**，不然檔案跟實際設定會對不起來。

### 兩個 Lambda（主控台建立，只做一次）

Lambda → 建立函數 → 容器映像 → 選 ECR 的 `traceable-research-assistant:latest`、x86_64、使用現有角色 `trace-app-worker-role`。建好後：

| | trace-app-worker | trace-app-sweeper |
|---|---|---|
| 映像組態 → CMD 覆寫 | `python,-m,awslambdaric,app.worker.handle_process_message` | `python,-m,awslambdaric,app.worker.handle_sweep` |
| 記憶體 / 逾時 | 1024 MB / 10 分鐘 | 512 MB / 2 分鐘 |
| 環境變數 | `S3_BUCKET_NAME`、`SQS_QUEUE_URL` | 同左（sweeper 一定要有 `SQS_QUEUE_URL`，沒有的話會在 Lambda 裡直接處理文件） |
| 觸發 | SQS 主佇列，批次大小 1、最大並行 2 | EventBridge 規則 `trace-app-sweeper-hourly`，`rate(1 hour)` |

CMD 覆寫：同一個 image，Fargate 照 Dockerfile 跑 uvicorn，Lambda 改跑 `awslambdaric` 交給 `app/worker.py` 裡指定的函式。密碼不用設環境變數，worker 啟動時自己去 Secrets Manager 讀（`app/core/lambda_secrets.py`）。

### push 新的 image 之後，要更新哪些東西

**ECS 和 Lambda 都不會自己跟著 `:latest` 走**，兩個都記的是當時那個 image 的內容指紋（digest）：

- **Lambda**：建立或 `update-function-code` 時查一次 `:latest`，之後固定用那個 image
- **ECS**：每次「建立部署（deployment）」時查一次，這次部署底下啟動的所有容器都用同一個 image（ECS 的 software version consistency）。**把 desired count 從 0 調回 1 不算新的部署**，會沿用上一次部署記下的舊 image

| push 新 image 之後 | 要做的事 |
|---|---|
| 兩個 Lambda | `update-function-code` × 2 |
| API（ECS） | `update-service --force-new-deployment`，**不管 API 現在開著還是關著** |

漏掉其中一個，API 和 worker 就會跑不同版本的程式碼。之後做 CI/CD（主線 C5）就是要把這整套自動化。

這是踩過才知道的：2026-10-15 A5 演練時，把 API 從 0 調回 1，啟動的容器跑的竟然是前一天的 image（因為那之後沒有 `--force-new-deployment` 過），一開始以為「關著的 API 下次開啟就會拿到新版」是錯的。

### 更新 Lambda 到新的 image（每次 push 新 image 都要做）

**Lambda 不會自動換成 ECR 上最新的 `:latest`**。建立（或上次更新）時，它記住的是當時那個 image 的內容指紋（digest），之後 push 新的 image 它不會知道。所以 build + push 之後，除了更新 ECS，兩個 Lambda 也要各更新一次：

```powershell
aws lambda update-function-code --function-name trace-app-worker --image-uri "${REPO}:latest" --region ap-southeast-2
aws lambda update-function-code --function-name trace-app-sweeper --image-uri "${REPO}:latest" --region ap-southeast-2
```

### 手動測試一個 Lambda（不會動到任何資料）

傳一個空的工單清單給 worker：能正常啟動、讀到密碼、載入程式碼，就會回傳 `null`。sweeper 會真的去查資料庫，沒有卡住的文件時回傳 `{"requeued": 0}`。

```bash
aws lambda invoke --function-name trace-app-worker --cli-binary-format raw-in-base64-out --payload '{"Records": []}' --region ap-southeast-2 out.json
```

### 檢查佇列、查 Lambda 的 log

```bash
aws sqs get-queue-attributes --queue-url https://sqs.ap-southeast-2.amazonaws.com/416113247224/trace-app-documents-dlq --attribute-names ApproximateNumberOfMessages --region ap-southeast-2
```
**dlq 裡有東西 = 有工單試了 3 次都失敗**，要去查原因。

Lambda 的 log 在 CloudWatch 的 `/aws/lambda/trace-app-worker`、`/aws/lambda/trace-app-sweeper`（Git Bash 一樣要先 `export MSYS_NO_PATHCONV=1`）。每次執行最後一行 `REPORT` 有執行時間；冷啟動的那次會多一個 `Init Duration`。

### Lambda、SQS 不用關

跟 Fargate 不一樣，沒有工作時不收費。測完只要把 ECS Service 調回 0。

---

## CloudWatch Logs：log 保留天數（C1，2026-10-16）

CloudWatch 預設永久保留 log，費用會一直累積。三個 log group 都設成保留 30 天（見 [decisions/010](decisions/010-結構化-log.md)）：

```bash
aws logs put-retention-policy --log-group-name /ecs/trace-app --retention-in-days 30 --region ap-southeast-2
aws logs put-retention-policy --log-group-name /aws/lambda/trace-app-worker --retention-in-days 30 --region ap-southeast-2
aws logs put-retention-policy --log-group-name /aws/lambda/trace-app-sweeper --retention-in-days 30 --region ap-southeast-2
```

確認（`retentionInDays` 應該是 30）：

```bash
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/trace-app --region ap-southeast-2 --query "logGroups[].[logGroupName,retentionInDays]"
aws logs describe-log-groups --log-group-name-prefix /ecs/trace-app --region ap-southeast-2 --query "logGroups[].[logGroupName,retentionInDays]"
```

### 查詢範例（CloudWatch 主控台 → Logs Insights）

選好 log group 之後貼上查詢。log 是 JSON，欄位可以直接用。

一份文件的完整經歷（API 和 worker 兩個 log group 一起選）：

```
fields @timestamp, event, duration_ms, reason
| filter document_id = 42
| sort @timestamp asc
```

各步驟平均花多久：

```
filter ispresent(duration_ms)
| stats count(*), avg(duration_ms), max(duration_ms) by event
```

---

## 前端：本機開發環境建置

`frontend/` 是獨立的 React 專案（Vite + TypeScript + Tailwind CSS v4 + shadcn/ui 風格的元件），跟 `app/` 的 Python 環境完全分開。新電腦要接手，裝好 Node.js 之後（見本文件最上面的前置工具表），跑：

```bash
cd frontend
npm install
```

裝好套件之後，日常開發用：

```bash
npm run dev
```

會在 `http://localhost:5173` 開一個開發用的伺服器，存檔會自動刷新畫面。**同時要把後端開著**（另一個終端機、另一個視窗，`uvicorn app.main:app --reload`，跑在 8000）——前端的 [`vite.config.ts`](../frontend/vite.config.ts) 裡設定了 proxy，會把 `/auth`、`/workspaces`、`/documents` 這幾個路徑的請求自動轉給 `localhost:8000`，所以瀏覽器那邊看起來像是同一個來源，不用另外設定 CORS。

### `shadcn` CLI 在這個環境的已知問題

官方的元件安裝工具 `npx shadcn@latest init` 在目前這個版本（4.x）、這個專案的設定組合下會出錯（寫完 `components.json` 之後讀不回來、或是把元件寫到一個叫 `@` 的錯誤資料夾，而不是 `src/components/ui/`）。研判是這個 CLI 版本本身的 bug（它最近改成預設用 Base UI 而不是 Radix 當底層，样式也換了一套「base-nova」，還不夠穩）。

**之後如果要加新元件**，不要用 `init`（會壞），`add` 要**明確指定 `--path`** 才不會寫到錯的資料夾：

```bash
npx shadcn@latest add <元件名稱> --yes --path src/components/ui
```

裝完之後**一定要打開檔案檢查 import 那幾行**——確認是 `import { cn } from "@/lib/utils"`，不是 `import { cn } from "cn"`；用到 Radix 元件的話是 `@radix-ui/react-xxx`，不是 `@base-ui/react/xxx`。如果是後者，代表又抓到新的「base-nova」樣式，要手動改成 Radix 版本（現有的 `src/components/ui/*.tsx` 都是照這個標準寫的，可以直接參考）。
