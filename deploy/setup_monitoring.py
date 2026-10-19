# 建立告警、帳單上限、儀表板（docs/decisions/015）
# 用法（在專案資料夾）：.venv\Scripts\python deploy/setup_monitoring.py 你的email
#
# 做的事跟在 AWS 主控台一個一個點出來一樣，只是寫成程式：可以重複執行（已經有的會被更新，不會重複建）、設定有紀錄
# 用的是你電腦上 AWS CLI 登入的帳號（chun-admin），不是程式的 .env
#
# 整體：
#   log（JSON）──metric filter──→ 數字（metric，保留 15 個月）──→ 告警 ──→ SNS ──→ email
#                                                              └──→ 儀表板
# 為什麼要轉成數字：log 只保留 30 天（decisions/010），數字才留得夠久，半年後還看得到整段運作紀錄

import json
import sys

import boto3

REGION = "ap-southeast-1"
ACCOUNT_ID = "416113247224"
NAMESPACE = "TraceApp"  # 我們自己的數字都放在這個名稱底下，跟 AWS 內建的分開
TOPIC_NAME = "trace-app-alerts"
DASHBOARD_NAME = "trace-app"
BUDGET_NAME = "trace-app-monthly"
BUDGET_USD = "5"

API, WORKER, SWEEPER = (f"/aws/lambda/trace-app-{name}" for name in ("api", "worker", "sweeper"))

# ---------- 1. 把 log 變成數字的規則 ----------
# (log group, 規則名稱, 篩選條件, 數字名稱, 每符合一筆加多少)
# 篩選條件的寫法：{ $.欄位 = "值" } 比對 JSON 的欄位；純文字的用 "字串" 比對
# 加多少：寫 "1" 就是數筆數；寫 "$.total_ms" 就是把那筆的 total_ms 加進去（用來算平均）
METRIC_FILTERS = [
    # 錯誤：三個 Lambda 只要出現 ERROR 等級的 log 就算一次
    (API, "errors", '{ $.level = "ERROR" }', "Errors", "1"),
    (WORKER, "errors", '{ $.level = "ERROR" }', "Errors", "1"),
    (SWEEPER, "errors", '{ $.level = "ERROR" }', "Errors", "1"),
    # 超過時間上限被 Lambda 強制停掉：程式沒機會寫 log，只有 Lambda 自己印的這行純文字
    (API, "timeouts", '"Task timed out"', "Errors", "1"),
    (WORKER, "timeouts", '"Task timed out"', "Errors", "1"),
    # OpenAI 永久性錯誤（key 失效、餘額用完）：問答和處理文件兩邊各自的寫法
    (API, "openai-permanent", '{ $.event = "qa_failed" && $.error_type = "permanent" }', "OpenAIPermanentErrors", "1"),
    (WORKER, "openai-permanent", '{ $.event = "failed" && $.error_type = "OpenAIPermanentError" }', "OpenAIPermanentErrors", "1"),
    # 全系統的每日用量到上限
    (API, "system-quota", '{ $.event = "quota_exceeded" && $.scope = "system" }', "SystemQuotaExceeded", "1"),
    # sweeper 成功巡邏一次（壞掉的話這個數字會停住，用來抓「巡邏沒在跑」）
    (SWEEPER, "sweeps", '{ $.event = "sweep" }', "Sweeps", "1"),
    # 儀表板用：系統平常在做什麼
    (API, "requests", '{ $.event = "request" }', "Requests", "1"),
    (API, "questions", '{ $.event = "qa_generate" }', "QuestionsAnswered", "1"),
    (WORKER, "processed", '{ $.event = "processed" }', "DocumentsProcessed", "1"),
    (WORKER, "processing-time", '{ $.event = "processed" }', "ProcessingMs", "$.total_ms"),
    (WORKER, "failed", '{ $.event = "failed" }', "DocumentsFailed", "1"),
    (API, "openai-cost", '{ $.event = "openai_usage" }', "OpenAICostUSD", "$.cost_usd"),
    (WORKER, "openai-cost", '{ $.event = "openai_usage" }', "OpenAICostUSD", "$.cost_usd"),
]


def metric(name, stat="Sum"):
    return {"Namespace": NAMESPACE, "MetricName": name, "Statistic": stat}


# ---------- 2. 告警 ----------
# 只抓「一定要人處理」的事。告警只在狀態改變時寄信（正常 → 告警寄一封、恢復正常再一封），問題持續也不會一直寄
# TreatMissingData：這段時間完全沒有數字時怎麼算。notBreaching = 沒事；breaching = 有事
ALARMS = [
    {
        "AlarmName": "trace-app-errors",
        "AlarmDescription": "出現 ERROR 等級的 log 或 Lambda 逾時：可能有 bug。到 CloudWatch Logs Insights 查 level = ERROR",
        **metric("Errors"),
        "Period": 300, "EvaluationPeriods": 1, "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
    },
    {
        "AlarmName": "trace-app-openai-permanent",
        "AlarmDescription": "OpenAI 永久性錯誤（key 失效、餘額用完）：AI 功能全部停了，要換 key 或儲值",
        **metric("OpenAIPermanentErrors"),
        "Period": 300, "EvaluationPeriods": 1, "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
    },
    {
        "AlarmName": "trace-app-system-quota",
        "AlarmDescription": "全系統今天的 OpenAI 用量到上限（5 美元）：所有人到半夜 12 點前都不能用，可能有人在濫用",
        **metric("SystemQuotaExceeded"),
        "Period": 300, "EvaluationPeriods": 1, "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
    },
    {
        "AlarmName": "trace-app-sweeper-stopped",
        "AlarmDescription": "sweeper 超過 3 小時沒有成功巡邏：卡住的文件和孤兒檔案會開始累積",
        **metric("Sweeps"),
        # 3 個 1 小時都是 0 次 → 告警。完全沒資料（sweeper 根本沒被叫醒）也算有事
        "Period": 3600, "EvaluationPeriods": 3, "Threshold": 1,
        "ComparisonOperator": "LessThanThreshold", "TreatMissingData": "breaching",
    },
    {
        "AlarmName": "trace-app-dlq",
        "AlarmDescription": "DLQ 裡有工單：某份文件連續 3 次處理到一半程式就死掉，系統放棄了。到 SQS 主控台看 trace-app-documents-dlq",
        # 這個不用從 log 轉，SQS 本來就有這個數字
        "Namespace": "AWS/SQS", "MetricName": "ApproximateNumberOfMessagesVisible", "Statistic": "Maximum",
        "Dimensions": [{"Name": "QueueName", "Value": "trace-app-documents-dlq"}],
        "Period": 300, "EvaluationPeriods": 1, "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
    },
]


# ---------- 3. 儀表板 ----------
# 每張圖以「一天」為單位，預設顯示最近 6 個月
def daily_graph(title, metrics, x, y, stat="Sum", width=12):
    return {
        "type": "metric", "x": x, "y": y, "width": width, "height": 6,
        "properties": {
            "title": title, "region": REGION, "view": "timeSeries", "stat": stat, "period": 86400,
            "metrics": [[NAMESPACE, name, {"label": label}] for name, label in metrics],
        },
    }


DASHBOARD = {
    "start": "-P180D",  # 最近 180 天
    "widgets": [
        {"type": "text", "x": 0, "y": 0, "width": 24, "height": 2, "properties": {"markdown": (
            "# trace-app 運作紀錄\n每一格是一天。資料從 log 轉來，保留 15 個月（log 本身只留 30 天）。"
            "告警設定和處理紀錄見 docs/decisions/015、docs/operations-log.md"
        )}},
        daily_graph("每天的請求數", [("Requests", "API 請求")], 0, 2),
        daily_graph("每天處理的文件", [("DocumentsProcessed", "成功"), ("DocumentsFailed", "失敗")], 12, 2),
        daily_graph("每天回答的問題", [("QuestionsAnswered", "問題")], 0, 8),
        daily_graph("每天的 OpenAI 花費（美元）", [("OpenAICostUSD", "花費")], 12, 8),
        daily_graph("處理一份文件平均花多久（毫秒）", [("ProcessingMs", "平均")], 0, 14, stat="Average"),
        daily_graph("錯誤", [("Errors", "ERROR / 逾時"), ("OpenAIPermanentErrors", "OpenAI 永久性錯誤")], 12, 14),
        {"type": "alarm", "x": 0, "y": 20, "width": 24, "height": 3, "properties": {
            "title": "告警狀態",
            "alarms": [f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:{a['AlarmName']}" for a in ALARMS],
        }},
    ],
}


def main(email):
    logs = boto3.client("logs", region_name=REGION)
    cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    # AWS Budgets 是全球服務，固定用 us-east-1
    budgets = boto3.client("budgets", region_name="us-east-1")

    print("1. 把 log 變成數字的規則")
    for log_group, name, pattern, metric_name, value in METRIC_FILTERS:
        # put_metric_filter：同名的規則已經存在就更新，所以重複執行沒關係
        logs.put_metric_filter(
            logGroupName=log_group,
            filterName=f"trace-app-{name}",
            filterPattern=pattern,
            metricTransformations=[{"metricName": metric_name, "metricNamespace": NAMESPACE, "metricValue": value}],
        )
        print(f"   {log_group:32} {metric_name}")

    print("2. 通知：SNS topic + email")
    # create_topic：同名的已經存在就直接回傳那一個
    topic_arn = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]
    existing = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
    if any(s["Endpoint"] == email for s in existing):
        print(f"   {email} 已經訂閱過了")
    else:
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        print(f"   已寄確認信到 {email}，要點信裡的連結才會開始收到告警")

    print("3. 告警")
    for alarm in ALARMS:
        # 進入告警、恢復正常時都通知
        cloudwatch.put_metric_alarm(**alarm, AlarmActions=[topic_arn], OKActions=[topic_arn])
        print(f"   {alarm['AlarmName']}")

    print("4. 帳單上限")
    budget = {
        "BudgetName": BUDGET_NAME,
        "BudgetLimit": {"Amount": BUDGET_USD, "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    }
    # 實際花費超過 100%、或預測這個月會超過，都寄信
    notifications = [
        {"Notification": {"NotificationType": kind, "ComparisonOperator": "GREATER_THAN", "Threshold": 100, "ThresholdType": "PERCENTAGE"},
         "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}]}
        for kind in ("ACTUAL", "FORECASTED")
    ]
    try:
        budgets.create_budget(AccountId=ACCOUNT_ID, Budget=budget, NotificationsWithSubscribers=notifications)
        print(f"   每月 {BUDGET_USD} 美元，超過或預測會超過就寄信")
    except budgets.exceptions.DuplicateRecordException:
        budgets.update_budget(AccountId=ACCOUNT_ID, NewBudget=budget)
        print(f"   已經存在，更新成每月 {BUDGET_USD} 美元")

    print("5. 儀表板")
    cloudwatch.put_dashboard(DashboardName=DASHBOARD_NAME, DashboardBody=json.dumps(DASHBOARD, ensure_ascii=False))
    print(f"   https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#dashboards/dashboard/{DASHBOARD_NAME}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit("用法：python deploy/setup_monitoring.py 你的email")
    main(sys.argv[1])
