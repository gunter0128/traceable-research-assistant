# 告警規則跟程式寫出的 log 對得上（docs/decisions/015、046 B3-3）
# CloudWatch 用篩選條件（deploy/setup_monitoring.py 的 METRIC_FILTERS）比對每一筆 JSON log，符合就把數字 +1，數字到門檻就寄告警
# 這裡不連 AWS：讓程式真的跑一遍出錯的流程（真的 OpenAI SDK、假的網路），收集它寫出的 log（跟 CloudWatch 收到的一樣），
# 再拿同一條篩選條件來比對：
#   金鑰失效（401）、餘額不足（429 + insufficient_quota）→ 一定要符合「OpenAI 永久性錯誤」，寄告警
#   暫時性的錯誤（503、超時）→ 不能符合（不然 OpenAI 抖一下就寄信），問答也不能寫 ERROR（會觸發另一個「有 bug」的告警）
# 有人改了錯誤的類別名稱、log 的寫法或篩選條件，三邊對不上時這裡會失敗

import re

import httpx2
import pytest

from deploy.setup_monitoring import API, METRIC_FILTERS, WORKER
from tests.helpers import (
    create_workspace,
    login_headers,
    openai_error,
    upload_pdf,
    use_fake_openai,
)
from tests.pdf_fixtures import valid_pdf_bytes


# METRIC_FILTERS 裡某條規則的篩選條件，例如 (WORKER, "openai-permanent")
def _pattern(log_group, rule_name):
    return next(pattern for group, name, pattern, _, _ in METRIC_FILTERS if group == log_group and name == rule_name)


# CloudWatch 篩選條件的一小部分：{ $.欄位 = "值" && $.欄位 = "值" }（每個欄位都要等於那個值）
# 用到其他寫法（||、數字比較……）時直接讓測試失敗，提醒這裡要跟著補
_TERM = re.compile(r'^\$\.(\w+) = "([^"]*)"$')


def _matches(pattern, entry):
    assert pattern.startswith("{") and pattern.endswith("}"), f"這裡還不支援這種篩選條件：{pattern}"
    for term in pattern[1:-1].split("&&"):
        found = _TERM.match(term.strip())
        assert found, f"這裡還不支援這種篩選條件：{term}"
        field, value = found.groups()
        if entry.get(field) != value:
            return False
    return True


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
