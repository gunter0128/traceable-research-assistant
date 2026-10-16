# 結構化 log（docs/decisions/010）：每一筆 log 是一行 JSON，API 和 Lambda 共用這一份
# 印到 stdout 就好：ECS 和 Lambda 都會把 stdout 自動收進 CloudWatch
# 檔名不叫 logging.py：會跟 Python 內建的 logging 撞名

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

# ContextVar：「這一次執行專屬」的變數。同時有好幾個請求在跑時，各自看到自己的值，不會互相蓋掉
# 在請求 / 工單一開始設好，之後這次執行裡寫的每一筆 log 都會自動帶上，不用每次手動傳
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
document_id_var: ContextVar[int | None] = ContextVar("document_id", default=None)

# 我們自己的 logger。不用最上層的 root logger：Lambda 的執行環境會在 root 上裝它自己的格式，
# 用自己的、而且不往上傳（propagate = False），同一筆才不會印兩次、格式也不會被改掉
_logger = logging.getLogger("app")


# 把一筆 log 轉成一行 JSON
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            # isoformat：轉成 "2026-10-16T08:01:02.120+00:00" 這種標準時間格式
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for name, var in (("request_id", request_id_var), ("document_id", document_id_var)):
            value = var.get()
            if value is not None:
                entry[name] = value
        # event() 傳進來的欄位，放在 record 的 fields 屬性上（見下面的 extra=）
        entry.update(getattr(record, "fields", {}))
        # 有附上例外的話，把完整的錯誤堆疊也記下來（只有預料之外的錯誤會附）
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        # ensure_ascii=False：中文照原樣輸出，不變成 處 這種編碼
        # default=str：遇到 JSON 不認得的型別（例如時間）就轉成字串，不會整筆 log 印不出來
        return json.dumps(entry, ensure_ascii=False, default=str)


# 程式啟動時呼叫一次（main.py、worker.py）。呼叫好幾次也只會裝一次
def setup() -> None:
    if _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


# 寫一筆 log：event("embed_batch", batch=1, chunks=100)
# **fields：收任意多個「名稱=值」，變成 JSON 裡的欄位
# exc_info=True：附上目前正在處理的例外（只能在 except 裡面用）
def event(name: str, level: int = logging.INFO, exc_info: bool = False, **fields) -> None:
    # extra=：塞額外的屬性到這筆 log 上，JsonFormatter 再把它拿出來
    _logger.log(level, name, exc_info=exc_info, extra={"fields": fields})


# 量一段程式花多久，結束時寫一筆 log，自動加上 duration_ms
#   with timed("downloaded") as fields:
#       content = download()
#       fields["size_bytes"] = len(content)   ← 執行中才知道的值，可以中途補進去
# 中途出錯就不寫（出錯的情況由接住錯誤的地方另外記）
@contextmanager
def timed(name: str, **fields) -> Iterator[dict]:
    # perf_counter：專門拿來量經過時間的時鐘，比 time.time() 精準
    start = time.perf_counter()
    yield fields
    event(name, duration_ms=elapsed_ms(start), **fields)


# 從 start（time.perf_counter() 的值）到現在過了幾毫秒
def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


# 設定這次執行的編號，之後的 log 都會自動帶上
# 兩個都一定會設（沒給就設成 None）：Lambda 會重複使用同一個程式處理下一張工單，
# 不清掉的話，上一張工單的編號會跟著出現在下一張的 log 裡
def bind(request_id: str | None = None, document_id: int | None = None) -> None:
    request_id_var.set(request_id)
    document_id_var.set(document_id)


# 只補上文件編號、不動請求編號：API 收到上傳時還不知道文件編號，建好紀錄之後才補
def bind_document(document_id: int) -> None:
    document_id_var.set(document_id)
