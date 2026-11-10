# 告警規則跟程式寫出的 log 對得上（docs/decisions/015、046 B3-3、B4）
# CloudWatch 用篩選條件（deploy/setup_monitoring.py 的 METRIC_FILTERS）比對每一筆 JSON log，符合就把數字 +1，數字到門檻就寄告警
# 這裡不連 AWS：讓程式真的跑一遍出錯的流程（真的 OpenAI SDK、假的網路），收集它寫出的 log（跟 CloudWatch 收到的一樣），
# 再拿同一條篩選條件來比對：
#   金鑰失效（401）、餘額不足（429 + insufficient_quota）→ 一定要符合「OpenAI 永久性錯誤」，寄告警
#   暫時性的錯誤（503、超時）→ 不能符合（不然 OpenAI 抖一下就寄信），問答也不能寫 ERROR（會觸發另一個「有 bug」的告警）
# 有人改了錯誤的類別名稱、log 的寫法或篩選條件，三邊對不上時這裡會失敗
# 另外檢查：篩選條件用到的事件名稱程式裡真的有寫（拿掉某個 log 時，不會留下永遠是 0 的數字）；
# 正常的上傳、問答會讓儀表板的數字有東西可算

import json
import re
from pathlib import Path

import httpx2
import pytest

from app.core import openai_client
from deploy.setup_monitoring import API, METRIC_FILTERS, WORKER
from tests.helpers import (
    create_workspace,
    fake_answer,
    fake_embed_query,
    fake_embed_texts,
    login_headers,
    openai_error,
    upload_pdf,
    use_fake_openai,
)
from tests.pdf_fixtures import valid_pdf_bytes


# METRIC_FILTERS 裡某條規則的篩選條件，例如 (WORKER, "openai-permanent")
def _pattern(log_group, rule_name):
    return next(pattern for group, name, pattern, _, _ in METRIC_FILTERS if group == log_group and name == rule_name)


# CloudWatch 篩選條件的一小部分：
#   { $.欄位 = "值" && $.欄位 = "值" }：每一項都要成立
#   ($.欄位 = "值" || $.欄位 = "值")：括號裡其中一個成立就好
# 用到其他寫法（數字比較、括號裡再有括號……）時直接讓測試失敗，提醒這裡要跟著補
_TERM = re.compile(r'^\$\.(\w+) = "([^"]*)"$')


def _matches(pattern, entry):
    assert pattern.startswith("{") and pattern.endswith("}"), f"這裡還不支援這種篩選條件：{pattern}"
    return all(_matches_any(part.strip(), entry) for part in pattern[1:-1].split("&&"))


# 一項條件：一個「欄位 = 值」，或括號裡用 || 串起來的好幾個
def _matches_any(part, entry):
    if part.startswith("(") and part.endswith(")"):
        part = part[1:-1]
    return any(_equals(term.strip(), entry) for term in part.split("||"))


def _equals(term, entry):
    found = _TERM.match(term)
    assert found, f"這裡還不支援這種篩選條件：{term}"
    field, value = found.groups()
    return entry.get(field) == value


# 這次收集到的 log 裡，有幾筆符合篩選條件（CloudWatch 的數字會加幾）
def _count(logs, pattern):
    return sum(_matches(pattern, entry) for entry in logs.entries)


PERMANENT = [
    pytest.param(openai_error(401, code="invalid_api_key"), id="key-invalid"),
    pytest.param(openai_error(429, code="insufficient_quota"), id="quota"),
]
TEMPORARY = [
    pytest.param(openai_error(503), id="503"),
    pytest.param(httpx2.ReadTimeout("等太久", request=httpx2.Request("POST", "https://api.openai.com/v1")), id="timeout"),
]


# 先確認比對的方法本身是對的：欄位都相等才算符合，少一個、值不一樣都不算
def test_matcher():
    pattern = '{ $.event = "failed" && $.error_type = "OpenAIPermanentError" }'
    assert _matches(pattern, {"event": "failed", "error_type": "OpenAIPermanentError", "level": "WARNING"})
    assert not _matches(pattern, {"event": "failed", "error_type": "OpenAITemporaryError"})
    assert not _matches(pattern, {"event": "qa_failed", "error_type": "OpenAIPermanentError"})
    assert not _matches(pattern, {"event": "failed"})

    either = '{ $.event = "qa_failed" && ($.error_type = "a" || $.error_type = "b") }'
    assert _matches(either, {"event": "qa_failed", "error_type": "a"})
    assert _matches(either, {"event": "qa_failed", "error_type": "b"})
    assert not _matches(either, {"event": "qa_failed", "error_type": "c"})
    assert not _matches(either, {"event": "failed", "error_type": "a"})


# 測試：部署的過渡期間，問答那條告警新舊兩種寫法都接受（docs/decisions/046 B4）
#   B3-3 之前的程式寫 error_type = "permanent"，之後寫 "OpenAIPermanentError"：先更新告警再部署，中間不會有空窗
#   暫時性錯誤新舊兩種寫法都不能符合
def test_qa_alarm_accepts_old_and_new_logs_during_deploy():
    pattern = _pattern(API, "openai-permanent")

    assert _matches(pattern, {"event": "qa_failed", "error_type": "OpenAIPermanentError"})
    assert _matches(pattern, {"event": "qa_failed", "error_type": "permanent"})
    assert not _matches(pattern, {"event": "qa_failed", "error_type": "OpenAITemporaryError"})
    assert not _matches(pattern, {"event": "qa_failed", "error_type": "temporary"})


# 程式裡寫了哪些 log 事件：搜 app/ 底下所有的 log.event("名稱"、log.timed("名稱"
def _events_written_by_app():
    app_dir = Path(__file__).parent.parent / "app"
    source = "\n".join(path.read_text(encoding="utf-8") for path in app_dir.rglob("*.py"))
    return set(re.findall(r'log\.(?:event|timed)\(\s*"(\w+)"', source))


# 測試：每一條篩選條件用到的事件（$.event = "..."），程式裡都還有在寫
# 拿掉某個 log 卻忘了改篩選條件的話，那個數字會永遠是 0，告警也永遠不會響，這裡會擋下來
def test_filters_only_use_events_the_app_writes():
    written = _events_written_by_app()
    used = {event for _, _, pattern, _, _ in METRIC_FILTERS for event in re.findall(r'\$\.event = "(\w+)"', pattern)}

    assert used, "METRIC_FILTERS 裡找不到任何 $.event 條件，檢查的方法可能壞了"
    assert used <= written, f"篩選條件用到、程式卻沒在寫的事件：{used - written}"


# ---------- 文件處理（worker 的 log）----------
# 本機沒有 SQS，上傳完直接在同一個請求裡處理，寫的 log 跟 worker 一樣


def _upload_with_openai(client, monkeypatch, email, failure):
    use_fake_openai(monkeypatch, "embedding_client", failure)
    headers = login_headers(client, email)
    ws_id = create_workspace(client, headers)
    return upload_pdf(client, headers, ws_id, valid_pdf_bytes()).json()


# 測試：處理文件時金鑰失效、餘額不足 → 文件標失敗，log 符合「OpenAI 永久性錯誤」的篩選條件（寄告警）
@pytest.mark.parametrize("failure", PERMANENT)
def test_processing_permanent_error_matches_alarm(client, logs, monkeypatch, sdk_sleeps, failure):
    document = _upload_with_openai(client, monkeypatch, "alice@example.com", failure)

    assert document["status"] == "failed"
    assert document["error_message"] == "系統設定有誤，請聯絡管理員"
    assert _count(logs, _pattern(WORKER, "openai-permanent")) == 1


# 測試：處理文件時 OpenAI 暫時出問題 → 文件標失敗，但不符合「OpenAI 永久性錯誤」（不寄告警）
@pytest.mark.parametrize("failure", TEMPORARY)
def test_processing_temporary_error_does_not_match_alarm(client, logs, monkeypatch, sdk_sleeps, failure):
    document = _upload_with_openai(client, monkeypatch, "bob@example.com", failure)

    assert document["status"] == "failed"
    assert document["error_message"] == "AI 服務暫時無法使用，請稍後按重試"
    assert _count(logs, _pattern(WORKER, "openai-permanent")) == 0


# ---------- 問答（API 的 log）----------
# 空的 workspace 就夠了：問題轉向量那一步就會出錯


def _ask_with_openai(client, monkeypatch, email, failure):
    use_fake_openai(monkeypatch, "embedding_client", failure)
    headers = login_headers(client, email)
    ws_id = create_workspace(client, headers)
    return client.post(f"/workspaces/{ws_id}/ask", json={"question": "?"}, headers=headers)


# 測試：問答時金鑰失效、餘額不足 → 503「系統設定有誤」，log 符合「OpenAI 永久性錯誤」的篩選條件（寄告警）
@pytest.mark.parametrize("failure", PERMANENT)
def test_qa_permanent_error_matches_alarm(client, logs, monkeypatch, sdk_sleeps, failure):
    response = _ask_with_openai(client, monkeypatch, "carol@example.com", failure)

    assert response.status_code == 503
    assert response.json()["detail"] == "系統設定有誤，請聯絡管理員"
    assert _count(logs, _pattern(API, "openai-permanent")) == 1


# 測試：問答時 OpenAI 暫時出問題 → 503「AI 服務暫時無法使用」，不符合「OpenAI 永久性錯誤」，也不符合「有 bug」（ERROR）
@pytest.mark.parametrize("failure", TEMPORARY)
def test_qa_temporary_error_does_not_match_alarms(client, logs, monkeypatch, sdk_sleeps, failure):
    response = _ask_with_openai(client, monkeypatch, "dave@example.com", failure)

    assert response.status_code == 503
    assert response.json()["detail"] == "AI 服務暫時無法使用，請稍後再試"
    assert _count(logs, _pattern(API, "openai-permanent")) == 0
    assert _count(logs, _pattern(API, "errors")) == 0


# ---------- 儀表板的數字 ----------


# 測試：正常上傳一份文件、問一個問題 → 儀表板用的篩選條件都有東西可算，而且要加總的欄位真的在 log 裡
#   例如「處理時間」是把 processed 那筆的 total_ms 加起來，「OpenAI 費用」是 openai_usage 的 cost_usd
#   正常流程不能碰到任何告警
def test_normal_flow_feeds_dashboard_and_triggers_no_alarm(client, logs, monkeypatch):
    monkeypatch.setattr(openai_client, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(openai_client, "embed_query", fake_embed_query)
    monkeypatch.setattr(openai_client, "generate_answer", lambda system, user, timeout: fake_answer("答案[1]"))
    headers = login_headers(client, "erin@example.com")
    ws_id = create_workspace(client, headers)
    assert upload_pdf(client, headers, ws_id, valid_pdf_bytes()).json()["status"] == "processed"
    assert client.post(f"/workspaces/{ws_id}/ask", json={"question": "?"}, headers=headers).status_code == 200

    for log_group, name, pattern, _, value in METRIC_FILTERS:
        if not pattern.startswith("{"):
            continue  # 純文字的篩選條件（Lambda 的 "Task timed out"）不是我們的 JSON log
        matched = [entry for entry in logs.entries if _matches(pattern, entry)]
        if name in ("requests", "questions", "processed", "processing-time", "openai-cost"):
            assert matched, f"{name}：正常流程應該要有 log 符合"
        if name in ("errors", "openai-permanent", "system-quota", "failed"):
            assert not matched, f"{name}：正常流程不應該符合"
        # 數字不是「筆數」而是某個欄位加總的（$.total_ms、$.cost_usd），那個欄位每一筆都要有、而且是數字
        if value.startswith("$."):
            field = value[2:]
            assert all(isinstance(entry.get(field), int | float) for entry in matched), f"{name}：有一筆沒有 {field}"


# ---------- 在 AWS 上驗語法用的檔案 ----------
# deploy/test-metric-filter-openai-permanent.json：部署前用 aws logs test-metric-filter 驗問答那條篩選條件
# （只比對、不會改 AWS 上的任何設定，用法見 docs/refactor/FOLLOW_UPS.md）
# 檔案裡的篩選條件要跟 setup_monitoring.py 的一模一樣；範例的 3 筆 log，第 1、2 筆要符合、第 3 筆不能符合


def test_aws_check_file_matches_monitoring_setup():
    check_file = Path(__file__).parent.parent / "deploy" / "test-metric-filter-openai-permanent.json"
    check = json.loads(check_file.read_text(encoding="utf-8"))

    assert check["filterPattern"] == _pattern(API, "openai-permanent")
    results = [_matches(check["filterPattern"], json.loads(message)) for message in check["logEventMessages"]]
    assert results == [True, True, False]
