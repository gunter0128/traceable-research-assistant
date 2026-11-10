# 部署指令小抄

部署到 AWS 用到的指令，整理起來備查。指令不像程式碼,下過之後不會留在專案裡,容易忘記在幹嘛、哪些要重複做、哪些只做一次。

## 開始之前:這台電腦需要先裝好的工具

換一台新電腦接手這個專案,git clone 下來之後,光有程式碼還不夠,這幾個工具都要另外裝,而且**都是裝給作業系統用的,不會跟著 git 走**:

| 工具 | 用途 | 下載 |
|---|---|---|
| Python 3.11 + venv | 跑後端(`requirements.txt`) | 通常電腦已有,沒有就去 [python.org](https://www.python.org/) |
| Docker Desktop | build/跑容器、之後部署用的 image | [docker.com](https://www.docker.com/products/docker-desktop/) |
| AWS CLI v2 | 操作 AWS(ECR、Lambda、Secrets Manager...) | [AWS 官方安裝頁](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),裝完要 `aws configure` 用 `chun-admin` 的金鑰登入 |
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
- `--provenance=false`——**Lambda 一定要加**。新版 Docker 預設會附加 provenance 資訊，image 會變成 Lambda 不接受的格式（ECR 上看到 `image.index` 就是中招了，正確的是 `image.manifest`）
- `-t` 後面是取的名字，沒指定標籤（`:版本`）時，Docker 自動補 `:latest`

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

## 從零建立整套環境（全部用 CLI）

現在的架構（2026-10-17 起，見 [decisions/013](decisions/013-固定網址與-api-改用-lambda.md)）：

```
CloudFront（https://dpozdeau5nd0p.cloudfront.net）
  ├─ /auth/*、/workspaces*、/documents/*、/usage/*、/docs → Lambda：trace-app-api（Function URL）
  └─ 其他 → S3：前端檔案
瀏覽器 ──直接上傳──→ S3：PDF
SQS → Lambda：trace-app-worker；每小時排程 → Lambda：trace-app-sweeper
```

三個 Lambda 用同一份 Docker image，只是啟動設定（`deploy/*-image-config.json`）不同。整條線上沒有需要一直開著的機器，**沒人用的時候不用關任何東西**。

這一段是 2026-10-16 搬到新加坡（[decisions/011](decisions/011-搬到新加坡.md)）、2026-10-17 改成 Lambda API 時實際跑過的指令整理，要重建的話照這一段做。`deploy/` 裡的檔案寫死了區域、bucket 名稱、網址，換區域或重建時要先改。

在專案資料夾、PowerShell 裡跑。開新視窗都要先設這三個變數：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$REGION = "ap-southeast-1"
$REPO = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/traceable-research-assistant"
```

### 1. ECR + image

```powershell
aws ecr create-repository --repository-name traceable-research-assistant --region $REGION
$PASSWORD = aws ecr get-login-password --region $REGION
docker login --username AWS --password $PASSWORD "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker build --platform linux/amd64 --provenance=false -t traceable-research-assistant .
docker tag traceable-research-assistant:latest "${REPO}:latest"
docker push "${REPO}:latest"
```

- `docker login`：ECR 的臨時通行證 12 小時有效，過期要重新登入。PowerShell 5.1 用 `|` 把密碼直接傳給 docker 時，會在尾端多一個換行，ECR 認不得（`400 Bad Request`），所以先存進 `$PASSWORD` 再傳
- `docker tag`：幫本機的 image 多貼一個包含 ECR 位址的名字，本體還是同一份；`docker push` 照名字裡的位址上傳，所以一定要先 tag 才能 push

### 2. 密碼

搬家時是從舊區域複製（`$value` 從舊區域 `get-secret-value` 讀出來）；從零開始的話，`--secret-string` 直接填值：

```powershell
foreach ($name in "database-url", "secret-key", "openai-api-key") { $value = aws secretsmanager get-secret-value --secret-id "trace-app/$name" --region <舊區域> --query SecretString --output text; aws secretsmanager create-secret --name "trace-app/$name" --secret-string $value --region $REGION --query Name --output text }
```

### 3. S3（放 PDF）

```powershell
$BUCKET = "traceable-research-assistant-$ACCOUNT-$REGION"
aws s3api create-bucket --bucket $BUCKET --region $REGION --create-bucket-configuration LocationConstraint=$REGION
```

`LocationConstraint` 不寫的話會建在美國。新 bucket 預設擋掉所有公開存取、自動加密。搬家時再用 `aws s3 sync s3://舊bucket/documents/ s3://$BUCKET/documents/ --source-region <舊區域> --region $REGION` 把檔案複製過來。允許瀏覽器直接上傳的 CORS 設定在第 7 步（要等 CloudFront 的網址）。

### 4. SQS + IAM

```powershell
aws sqs create-queue --queue-name trace-app-documents-dlq --attributes file://deploy/sqs-dlq-attributes.json --region $REGION
aws sqs create-queue --queue-name trace-app-documents-queue --attributes file://deploy/sqs-queue-attributes.json --region $REGION

aws iam create-role --role-name trace-app-worker-role --assume-role-policy-document file://deploy/lambda-trust-policy.json --query "Role.RoleName"
aws iam attach-role-policy --role-name trace-app-worker-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name trace-app-worker-role --policy-name trace_app_worker_permissions --policy-document file://deploy/lambda-permissions-policy.json

aws iam create-role --role-name trace-app-api-role --assume-role-policy-document file://deploy/lambda-trust-policy.json --query "Role.RoleName"
aws iam attach-role-policy --role-name trace-app-api-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name trace-app-api-role --policy-name trace_app_api_permissions --policy-document file://deploy/api-lambda-permissions-policy.json

aws iam create-policy-version --policy-arn "arn:aws:iam::${ACCOUNT}:policy/S3AccessForTraceApp" --policy-document file://deploy/local-dev-s3-policy.json --set-as-default
```

- DLQ 要先建，主佇列的設定裡要指向它
- IAM 是全球共用的：只是換區域的話角色不用重建，只要 `put-role-policy` 更新權限內容
- API 和 worker 各一個角色：API 要能「上傳檔案」（才能簽上傳許可），worker 要能「從 SQS 拿工單」，各給各的
- 最後一行是本機開發用的 `trace-app-s3` 帳號的權限（managed policy，改它要新增版本；最多 5 個版本，滿了要先刪舊的）
- 新建的角色要等約 10 秒才能給 Lambda 用，太快建 Lambda 會出現「無法使用這個角色」，再跑一次就好

### 5. 三個 Lambda、觸發、排程

```powershell
foreach ($name in "api", "worker", "sweeper") { aws logs create-log-group --log-group-name "/aws/lambda/trace-app-$name" --region $REGION; aws logs put-retention-policy --log-group-name "/aws/lambda/trace-app-$name" --retention-in-days 30 --region $REGION }

aws lambda create-function --function-name trace-app-api --package-type Image --code "ImageUri=${REPO}:latest" --role "arn:aws:iam::${ACCOUNT}:role/trace-app-api-role" --memory-size 1024 --timeout 60 --image-config file://deploy/api-lambda-image-config.json --environment file://deploy/lambda-environment.json --region $REGION --query "State"
aws lambda create-function --function-name trace-app-worker --package-type Image --code "ImageUri=${REPO}:latest" --role "arn:aws:iam::${ACCOUNT}:role/trace-app-worker-role" --memory-size 1024 --timeout 600 --image-config file://deploy/lambda-worker-image-config.json --environment file://deploy/lambda-environment.json --region $REGION --query "State"
aws lambda create-function --function-name trace-app-sweeper --package-type Image --code "ImageUri=${REPO}:latest" --role "arn:aws:iam::${ACCOUNT}:role/trace-app-worker-role" --memory-size 512 --timeout 120 --image-config file://deploy/lambda-sweeper-image-config.json --environment file://deploy/lambda-environment.json --region $REGION --query "State"
foreach ($name in "api", "worker", "sweeper") { aws lambda wait function-active-v2 --function-name "trace-app-$name" --region $REGION }

# API 的固定網址（Function URL），公開：API 自己有登入檢查（decisions/013）。AWS 規定兩個權限都要給
aws lambda create-function-url-config --function-name trace-app-api --auth-type NONE --region $REGION --query "FunctionUrl"
aws lambda add-permission --function-name trace-app-api --statement-id public-url --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE --region $REGION --query "Statement"
aws lambda add-permission --function-name trace-app-api --statement-id public-url-invoke --action lambda:InvokeFunction --principal "*" --invoked-via-function-url --region $REGION --query "Statement"

# SQS → worker；每小時 → sweeper
aws lambda create-event-source-mapping --function-name trace-app-worker --event-source-arn "arn:aws:sqs:${REGION}:${ACCOUNT}:trace-app-documents-queue" --batch-size 1 --scaling-config MaximumConcurrency=2 --region $REGION --query "State"
aws events put-rule --name trace-app-sweeper-hourly --schedule-expression "rate(1 hour)" --region $REGION --query "RuleArn"
aws lambda add-permission --function-name trace-app-sweeper --statement-id eventbridge-hourly --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn "arn:aws:events:${REGION}:${ACCOUNT}:rule/trace-app-sweeper-hourly" --region $REGION --query "Statement"
aws events put-targets --rule trace-app-sweeper-hourly --targets file://deploy/sweeper-schedule-target.json --region $REGION
```

- Lambda 的 image 設定、環境變數、排程目標都放在 `deploy/` 的 JSON 檔：直接寫在指令裡的話，PowerShell 5.1 會把 JSON 的雙引號吃掉
- API 的時間上限 60 秒，跟 CloudFront 等待的上限一樣
- 新建的 Lambda **第一次**啟動會特別慢（要先從 ECR 拉整個 image，2026-10-17 實測 14 秒，超過 10 秒的準備上限被重來一次），之後的冷啟動約 3.4 秒
- `add-permission`：Lambda 預設誰都不能呼叫，要明確允許。主控台設排程時會自動做這步，CLI 要自己做

### 6. 前端 + CloudFront

```powershell
aws s3api create-bucket --bucket "traceable-research-assistant-frontend-$ACCOUNT" --region $REGION --create-bucket-configuration LocationConstraint=$REGION
cd frontend; npm run build; aws s3 sync dist "s3://traceable-research-assistant-frontend-$ACCOUNT" --delete --region $REGION; cd ..

aws cloudfront create-origin-access-control --origin-access-control-config file://deploy/cloudfront-frontend-oac.json --query "OriginAccessControl.Id" --output text
# ↑ 印出的 ID、和第 5 步印出的 Function URL 網域，填進 deploy/cloudfront-distribution.json
aws cloudfront create-distribution --distribution-config file://deploy/cloudfront-distribution.json --query "Distribution.[Id,DomainName,Status]" --output text
```

- 前端的 bucket 不公開，CloudFront 用 OAC 這個身分讀它
- `cloudfront-distribution.json` 是「路徑 → 轉給誰」的對照表：API 的路徑轉給 Lambda、不快取；其他轉給前端的 bucket、會快取
- 建好之後要 5～10 分鐘才會變成 `Deployed`：`aws cloudfront get-distribution --id <ID> --query "Distribution.Status" --output text`

### 7. 最後兩個權限設定

把第 6 步印出的 CloudFront ID 填進 `deploy/frontend-bucket-policy.json`、網址填進 `deploy/documents-bucket-cors.json`：

```powershell
aws s3api put-bucket-policy --bucket "traceable-research-assistant-frontend-$ACCOUNT" --policy file://deploy/frontend-bucket-policy.json --region $REGION
aws s3api put-bucket-cors --bucket "traceable-research-assistant-$ACCOUNT-$REGION" --cors-configuration file://deploy/documents-bucket-cors.json --region $REGION
```

- 前端的 bucket：只允許這一個 CloudFront 讀
- PDF 的 bucket：允許從 CloudFront 網址和本機開發（`http://localhost:5173`）打開的網頁直接上傳（只開放 POST）

### 8. 驗證

```powershell
'{"health_check": true}' | Out-File -Encoding ascii health_check.json
aws lambda invoke --function-name trace-app-worker --payload fileb://health_check.json --region $REGION out.json
Get-Content out.json
```

worker 真的對資料庫查一次 `SELECT 1`，`out.json` 是 `{"database": "ok"}` 才對；連不上資料庫的話會是 `errorMessage`（指令本身還是會成功結束，要看內容）。再用瀏覽器打開 CloudFront 的網址，登入、上傳、問答各試一次。

（以前這一步用 `{"probe": true}` 量 Lambda 到資料庫的速度（[decisions/011](decisions/011-搬到新加坡.md)），量完就用不到了，[decisions/046](decisions/046-簡化上雲後的可靠性機制.md) B4 拿掉。）

---

## 每次發布新版本

**2026-10-18 起由 GitHub Actions 自動做**（[decisions/014](decisions/014-ci-cd.md)）：push 到 main，測試通過就自動部署有改到的部分。執行紀錄在 GitHub 專案的 **Actions** 分頁。

### GitHub Actions 的 AWS 權限（只做一次）

```powershell
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
aws iam create-role --role-name trace-app-deploy-role --assume-role-policy-document file://deploy/github-deploy-trust-policy.json --query "Role.Arn"
aws iam put-role-policy --role-name trace-app-deploy-role --policy-name trace_app_deploy_permissions --policy-document file://deploy/github-deploy-permissions-policy.json
```

- 第 1 行：讓 AWS 信任 GitHub 發的證明（OIDC）。整個帳號只要建一次
- 第 2 行：部署用的角色，只信任 `gunter0128/traceable-research-assistant` 的 main 分支
  - 信任條件要寫 GitHub 實際送來的格式：`repo:帳號@帳號編號/專案@專案編號:ref:refs/heads/main`。不確定的話，先部署一次讓它失敗，到 CloudTrail 查 `AssumeRoleWithWebIdentity` 的紀錄，`userIdentity` 裡就是實際的值
- 第 3 行：只給部署需要的權限（推 image、更新和叫醒 Lambda、讀資料庫網址、上傳前端、清快取）

### 退回上一版

image 的標籤是 commit 編號。把三個 Lambda 指回上一個能用的版本（上線過的版本和 digest 記在 [operations-log.md](operations-log.md) 的「回退用的版本紀錄」）：

```powershell
foreach ($name in "api", "worker", "sweeper") { aws lambda update-function-code --function-name "trace-app-$name" --image-uri "${REPO}:<上一個 commit 編號>" --region $REGION --query "LastUpdateStatus" }
# 或用 digest 指定（標籤可以被改指到別的 image，digest 不會）：--image-uri "${REPO}@sha256:<digest>"
```

前端：在那個 commit 的 `frontend/` 跑 `npm ci`、`npm run build`，再照下面「手動部署」的前端指令上傳、清 CloudFront 的快取。

### 手動部署（GitHub Actions 出問題時）

下面是 CI/CD 之前手動發布的指令，GitHub Actions 做的就是這些。漏掉其中一步，各部分就會跑不同版本的程式碼。

**後端**（改了 `app/`、`requirements.txt`、`Dockerfile`）：

```powershell
$PASSWORD = aws ecr get-login-password --region $REGION
docker login --username AWS --password $PASSWORD "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker build --platform linux/amd64 --provenance=false -t traceable-research-assistant .
docker tag traceable-research-assistant:latest "${REPO}:latest"
docker push "${REPO}:latest"
foreach ($name in "api", "worker", "sweeper") { aws lambda update-function-code --function-name "trace-app-$name" --image-uri "${REPO}:latest" --region $REGION --query "LastUpdateStatus" }
```

確認 push 的最後一行有 `latest: digest: sha256:...` 才更新 Lambda。Lambda 記住的是 image 的內容指紋，不是 `:latest` 這個名字，**不更新的話會一直跑舊版**。

改了資料庫的 model 的話，先跑 `alembic upgrade head`（連正式資料庫），再更新 Lambda。

**前端**（改了 `frontend/`）：

```powershell
cd frontend; npm run build; aws s3 sync dist "s3://traceable-research-assistant-frontend-$ACCOUNT" --delete --region $REGION; cd ..
aws cloudfront create-invalidation --distribution-id E2IPB9CUGG6K2W --paths "/*" --query "Invalidation.Status" --output text
```

`create-invalidation`：CloudFront 會把前端檔案暫存在各地的機房，不讓它失效的話，使用者最久一天內都還會看到舊版。

---

## 常用的檢查指令

開新視窗一樣先設 `$ACCOUNT`、`$REGION`、`$REPO`（見「從零建立整套環境」）。指令裡的 `<上一個 commit 編號>` 這種寫法是佔位符號，要換成真的值：PowerShell 會把 `<` 當成特殊符號，照打會報錯。

ECR 上有哪些 image（`imageStatus: "ACTIVE"` 代表確實存在、可以用）：

```powershell
aws ecr describe-images --repository-name traceable-research-assistant --region $REGION
```

dead-letter queue 裡有沒有工單。**有東西 = 有工單試了 3 次都失敗**，要去查原因（也會收到告警，見下面）：

```powershell
aws sqs get-queue-attributes --queue-url "https://sqs.$REGION.amazonaws.com/$ACCOUNT/trace-app-documents-dlq" --attribute-names ApproximateNumberOfMessages --region $REGION
```

手動叫一次 worker、不處理任何文件：能正常啟動、讀到密碼、連得到資料庫，`out.json` 就是 `{"database": "ok"}`：

```powershell
'{"health_check": true}' | Out-File -Encoding ascii health_check.json
aws lambda invoke --function-name trace-app-worker --payload fileb://health_check.json --region $REGION out.json
Get-Content out.json
```

不要用 `{"Records": []}`：沒有工單時 worker 什麼都不做，連資料庫都沒碰，資料庫壞了也會回成功。

換密碼用 `update-secret`，不要刪掉重建（刪除預設有 30 天緩衝期，同名的會建不起來）。密碼只在 Lambda 冷啟動時讀一次，熱的 Lambda 還在用舊的，要重新部署一次才會全部換掉：

```powershell
aws secretsmanager update-secret --secret-id trace-app/database-url --secret-string "新值" --region $REGION
```

---

## CloudWatch Logs

三個 Lambda 的 log 在 `/aws/lambda/trace-app-api`、`/aws/lambda/trace-app-worker`、`/aws/lambda/trace-app-sweeper`，保留 30 天（第 5 步設定的，見 [decisions/010](decisions/010-結構化-log.md)：CloudWatch 預設永久保留，費用會一直累積）。每次執行最後一行 `REPORT` 有執行時間；冷啟動的那次會多一個 `Init Duration`。

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

## 告警、帳單上限、儀表板（C7，2026-10-19）

全部寫在 [deploy/setup_monitoring.py](../deploy/setup_monitoring.py)，在專案資料夾執行（用的是 AWS CLI 登入的帳號）：

```powershell
.venv\Scripts\python deploy/setup_monitoring.py 你的email
```

- 可以重複執行：已經有的會被更新，不會重複建立。改了腳本裡的設定，再執行一次就套用
- 第一次執行後，信箱會收到 AWS 的確認信，**要點信裡的連結**才會開始收到告警
- 儀表板：CloudWatch 主控台 → 儀表板 → `trace-app`
- 告警：CloudWatch 主控台 → 告警
- 帳單上限：Billing 主控台 → Budgets → `trace-app-monthly`
- 收到告警、處理完之後，在 [operations-log.md](operations-log.md) 記一筆

設計和每個告警的意思見 [decisions/015](decisions/015-告警與運作紀錄.md)。

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

會在 `http://localhost:5173` 開一個開發用的伺服器，存檔會自動刷新畫面。**同時要把後端開著**（另一個終端機、另一個視窗，`uvicorn app.main:app --reload`，跑在 8000）——前端的 [`vite.config.ts`](../frontend/vite.config.ts) 裡設定了 proxy，會把 `/auth`、`/workspaces`、`/documents`、`/usage` 這幾個路徑的請求自動轉給 `localhost:8000`，所以瀏覽器那邊看起來像是同一個來源，不用另外設定 CORS。

### `shadcn` CLI 在這個環境的已知問題

官方的元件安裝工具 `npx shadcn@latest init` 在目前這個版本（4.x）、這個專案的設定組合下會出錯（寫完 `components.json` 之後讀不回來、或是把元件寫到一個叫 `@` 的錯誤資料夾，而不是 `src/components/ui/`）。研判是這個 CLI 版本本身的 bug（它最近改成預設用 Base UI 而不是 Radix 當底層，样式也換了一套「base-nova」，還不夠穩）。

**之後如果要加新元件**，不要用 `init`（會壞），`add` 要**明確指定 `--path`** 才不會寫到錯的資料夾：

```bash
npx shadcn@latest add <元件名稱> --yes --path src/components/ui
```

裝完之後**一定要打開檔案檢查 import 那幾行**——確認是 `import { cn } from "@/lib/utils"`，不是 `import { cn } from "cn"`；用到 Radix 元件的話是 `@radix-ui/react-xxx`，不是 `@base-ui/react/xxx`。如果是後者，代表又抓到新的「base-nova」樣式，要手動改成 Radix 版本（現有的 `src/components/ui/*.tsx` 都是照這個標準寫的，可以直接參考）。
