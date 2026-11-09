# 文件處理的轉向量 openai_client.embed_texts（docs/decisions/046 B3-1）
# 用真的 OpenAI SDK，只把網路換成假的（httpx2.MockTransport）：SDK 內建的重試、等待上限、錯誤都是真的在跑，
# 但請求不會送到 OpenAI、不花錢。不用資料庫

import json
import logging

import httpx2
import openai
import pytest

# SDK 內部的模組：重試前的 time.sleep、jitter 用的 random 都在這裡
from openai import _base_client as sdk

# OpenAI 叫我們等的時候，SDK 最多照等幾秒（120）
from openai._constants import MAX_RETRY_AFTER_DELAY

from app.core import log, openai_client
from app.core.openai_client import Usage
from tests.helpers import LogCollector

CLOUD_URL = "https://api.openai.com/v1"
LOCAL_URL = "http://gpu-server:8000/v1"  # 企業內部版自己架的轉向量服務（vLLM，docs/decisions/037）


# 轉向量成功的回應。故意把順序弄反（第 2 段在前面），測得出 embed_texts 有照 index 放回原本的位置
def _ok():
    return httpx2.Response(200, json={
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [
            {"object": "embedding", "index": 1, "embedding": [1.0, 1.0]},
            {"object": "embedding", "index": 0, "embedding": [0.0, 0.0]},
        ],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    })


# 錯誤的回應：狀態碼 + OpenAI 的錯誤代碼（例如 insufficient_quota）+ header（例如 retry-after）
def _error(status, code=None, headers=None):
    return httpx2.Response(status, headers=headers, json={"error": {"message": "模擬錯誤", "code": code}})


def _connection_error():
    return httpx2.ConnectError("連不上", request=httpx2.Request("POST", CLOUD_URL))


def _read_timeout():
    return httpx2.ReadTimeout("等太久", request=httpx2.Request("POST", CLOUD_URL))


# 假的 OpenAI 伺服器：照順序回應清單裡的東西，清單用完了就一直回最後一個（例如「一直 503」）
# 清單裡放例外的話就丟出來，模擬連不上、超時
class FakeServer:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    # MockTransport 每收到一個請求就呼叫這裡
    def handle(self, request):
        self.requests.append(request)
        result = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


# 把 openai_client.embedding_client 換成連到假伺服器的真 SDK client
# 網址、max_retries=0 跟正式的 client 一樣（預設不重試，embed_texts 自己用 with_options 打開），只有網路是假的
def _use_fake_server(monkeypatch, *responses, base_url=CLOUD_URL):
    server = FakeServer(list(responses))
    client = openai.OpenAI(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(server.handle)),
    )
    monkeypatch.setattr(openai_client, "embedding_client", client)
    return server


# SDK 重試前會 time.sleep：換成只記錄要等幾秒，測試才不用真的等
# （time 是共用的模組，所以這段測試期間所有的 time.sleep 都被換掉，monkeypatch 會在測試結束時換回來）
# jitter 的隨機數固定成 0，間隔就剛好是 0.5、1 秒
@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(sdk.time, "sleep", waited.append)
    monkeypatch.setattr(sdk, "random", lambda: 0)
    return waited


# ---------- 成功 ----------


# 測試：向量照輸入的順序排好、回傳這次的用量；雲端（OpenAI）和地端（自己架的服務）走同一段程式
@pytest.mark.parametrize("base_url", [CLOUD_URL, LOCAL_URL])
def test_returns_vectors_in_input_order_with_usage(monkeypatch, base_url):
    server = _use_fake_server(monkeypatch, _ok(), base_url=base_url)

    vectors, usage = openai_client.embed_texts(["第一段", "第二段"])

    assert vectors == [[0.0, 0.0], [1.0, 1.0]]
    assert usage == Usage(openai_client.EMBEDDING_MODEL, input_tokens=7)
    assert str(server.requests[0].url) == f"{base_url}/embeddings"
    assert json.loads(server.requests[0].content)["input"] == ["第一段", "第二段"]


# 測試：每次請求的等待上限是 PROCESSING_TIMEOUT（30 秒）
# httpx 的上限分四種（連線、每一次讀取、每一次寫入、等連線池），四種都是 30 秒；不是「整個請求最多 30 秒」
def test_each_request_uses_processing_timeout(monkeypatch):
    server = _use_fake_server(monkeypatch, _ok())

    openai_client.embed_texts(["第一段", "第二段"])

    timeout = openai_client.PROCESSING_TIMEOUT
    assert server.requests[0].extensions["timeout"] == {"connect": timeout, "read": timeout, "write": timeout, "pool": timeout}


# ---------- SDK 內建的重試 ----------


# 測試：暫時性的錯誤 → SDK 自己重試，間隔 0.5、1 秒，第 3 次成功就正常回傳
def test_temporary_errors_are_retried(monkeypatch, sleeps):
    server = _use_fake_server(monkeypatch, _error(500), _connection_error(), _ok())

    vectors, _ = openai_client.embed_texts(["第一段", "第二段"])

    assert vectors == [[0.0, 0.0], [1.0, 1.0]]
    assert len(server.requests) == 3
    assert sleeps == [0.5, 1.0]


# 測試：一直失敗 → 一共試 PROCESSING_RETRIES + 1 次（3 次）就放棄，丟暫時性錯誤（文件標失敗、使用者稍後按重試）
# SDK 原本的錯誤接在後面（__cause__），log 的錯誤堆疊裡看得到真正的原因
@pytest.mark.parametrize(
    ("response", "sdk_error"),
    [
        (_error(503), openai.InternalServerError),
        (_error(429), openai.RateLimitError),
        (_connection_error(), openai.APIConnectionError),
        (_read_timeout(), openai.APITimeoutError),
    ],
)
def test_gives_up_after_retries_as_temporary_error(monkeypatch, sleeps, response, sdk_error):
    server = _use_fake_server(monkeypatch, response)

    with pytest.raises(openai_client.OpenAITemporaryError) as raised:
        openai_client.embed_texts(["第一段"])

    assert len(server.requests) == openai_client.PROCESSING_RETRIES + 1
    assert isinstance(raised.value.__cause__, sdk_error)


# 測試：永久性錯誤（金鑰無效、請求格式錯誤）→ SDK 不重試，馬上丟永久性錯誤（文件標「系統設定有誤」、告警）
@pytest.mark.parametrize("status", [400, 401])
def test_permanent_error_is_not_retried(monkeypatch, sleeps, status):
    server = _use_fake_server(monkeypatch, _error(status))

    with pytest.raises(openai_client.OpenAIPermanentError):
        openai_client.embed_texts(["第一段"])

    assert len(server.requests) == 1
    assert sleeps == []


# 測試：餘額不足（429 + insufficient_quota）→ 最後還是歸類成永久性錯誤，告警看得到
# SDK 只看狀態碼，分不出餘額不足和速率限制，所以會先重試 2 次（多等約 1.5 秒）
# OpenAI 有在 header 說「不要重試」（x-should-retry: false）的話，SDK 就不重試
@pytest.mark.parametrize(("headers", "requests"), [(None, 3), ({"x-should-retry": "false"}, 1)])
def test_insufficient_quota_ends_as_permanent_error(monkeypatch, sleeps, headers, requests):
    server = _use_fake_server(monkeypatch, _error(429, code="insufficient_quota", headers=headers))

    with pytest.raises(openai_client.OpenAIPermanentError):
        openai_client.embed_texts(["第一段"])

    assert len(server.requests) == requests


# 測試：OpenAI 說「請等 N 秒」→ 120 秒以內 SDK 照等
@pytest.mark.parametrize("seconds", [20, MAX_RETRY_AFTER_DELAY])
def test_follows_retry_after(monkeypatch, sleeps, seconds):
    _use_fake_server(monkeypatch, _error(429, headers={"retry-after": str(seconds)}), _ok())

    openai_client.embed_texts(["第一段", "第二段"])

    assert sleeps == [seconds]


# 測試：OpenAI 叫我們等超過 120 秒 → SDK 不等也不重試，直接丟暫時性錯誤
# 所以兩次請求之間最多等 120 秒，心跳期限的估算用的就是這個數字（test_heartbeat_timeout_covers_one_batch_with_all_retries）
def test_does_not_wait_longer_than_sdk_limit(monkeypatch, sleeps):
    server = _use_fake_server(monkeypatch, _error(429, headers={"retry-after": str(MAX_RETRY_AFTER_DELAY + 1)}), _ok())

    with pytest.raises(openai_client.OpenAITemporaryError):
        openai_client.embed_texts(["第一段"])

    assert len(server.requests) == 1
    assert sleeps == []


# ---------- 重試的 log ----------


# 測試：SDK 每次重試寫的那筆 log，也會印成我們的 JSON、帶上文件編號（app/core/log.py 的 setup），CloudWatch 查得到
def test_sdk_retry_is_logged_as_json(monkeypatch, sleeps):
    log.setup()
    openai_logger = logging.getLogger("openai")
    # setup 有把 SDK 的 logger 接到我們的 JSON 格式、INFO 等級、不往上傳
    assert any(isinstance(handler.formatter, log.JsonFormatter) for handler in openai_logger.handlers)
    assert openai_logger.level == logging.INFO
    assert openai_logger.propagate is False

    collector = LogCollector()
    openai_logger.addHandler(collector)
    _use_fake_server(monkeypatch, _error(500), _ok())
    try:
        log.bind(document_id=42)
        openai_client.embed_texts(["第一段", "第二段"])
    finally:
        openai_logger.removeHandler(collector)
        log.bind()  # 清掉文件編號，不要帶到後面的測試

    assert len(collector.entries) == 1
    entry = collector.entries[0]
    assert entry["level"] == "INFO"
    assert entry["event"] == "Retrying request in 0.500000 seconds (retry 1 of 2)"
    assert entry["document_id"] == 42
