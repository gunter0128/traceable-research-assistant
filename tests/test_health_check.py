# 部署後的健康檢查（docs/decisions/046 B4）：CI 呼叫 worker Lambda 傳 {"health_check": true}，
# worker 真的對資料庫查一次 SELECT 1，CI 再看回傳的內容判斷成功還是失敗（.github/workflows/ci-cd.yml 的「熱身 + 確認能用」）
# 測的是：連得到資料庫才回成功；連不上就丟出錯誤，不會假裝正常；CI 的判斷條件跟回傳的內容對得上

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import worker

CI_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci-cd.yml"


# CI 用來判斷成功的 grep 條件，直接從 ci-cd.yml 讀出來：改了其中一邊、另一邊沒跟上，這裡會失敗
def _ci_success_pattern():
    found = re.search(r"grep -q '([^']+)' out\.json", CI_WORKFLOW.read_text(encoding="utf-8"))
    assert found, "ci-cd.yml 裡找不到檢查健康檢查結果的 grep"
    return found.group(1)


# 測試：連得到資料庫 → 回 {"database": "ok"}（用的是測試資料庫，SELECT 1 不需要資料表）
def test_health_check_queries_database():
    assert worker.handle_process_message({"health_check": True}, None) == {"database": "ok"}


# 測試：連不上資料庫 → 丟出錯誤（Lambda 這次呼叫算失敗），不會回成功
# 連到本機一個沒有人在聽的 port，馬上就會被拒絕，不用等
def test_health_check_fails_when_database_is_unreachable(monkeypatch):
    unreachable = create_engine("postgresql+psycopg://user:pw@127.0.0.1:1/none", connect_args={"connect_timeout": 2})
    monkeypatch.setattr(worker, "SessionLocal", sessionmaker(bind=unreachable))

    with pytest.raises(OperationalError):
        worker.handle_process_message({"health_check": True}, None)


# 測試：CI 的判斷條件
#   成功時 Lambda 把回傳值轉成 JSON 寫進 out.json → 要符合
#   Lambda 裡面出錯時，out.json 是錯誤訊息（errorMessage、errorType）→ 不能符合，CI 才會失敗
# grep 的條件在這裡用 Python 的 re 比對（這個條件裡只有一般字元和「空白重複幾次」，兩邊的意思一樣）
def test_ci_check_matches_only_success():
    pattern = re.compile(_ci_success_pattern())
    success = json.dumps(worker._check_database())
    failure = json.dumps({"errorMessage": "connection refused", "errorType": "OperationalError", "stackTrace": []})

    assert pattern.search(success)
    assert not pattern.search(failure)
