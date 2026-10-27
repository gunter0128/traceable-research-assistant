# 問實驗室的 Qwen（出題、寫示範答案、評審都用這個）：結果存起來、連不上就等一下再試
# 原本寫在 make_answers.py；第 2 批出題（041）也要用，搬出來兩邊共用

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import openai
from openai import OpenAI


# 每一次問模型的結果都存起來（照「送了什麼」當鑰匙），做完一筆就寫進檔案
# 第一次跑時，老師寫完 267 題（3 小時）之後轉接斷線，程式中斷、什麼都沒存到；現在中斷了重跑，做過的直接拿來用
class Cache:
    def __init__(self, path: Path):
        self.path, self.lock, self.done = path, threading.Lock(), {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    record = json.loads(line)
                    self.done[record["key"]] = record["value"]

    def get(self, key: str):
        return self.done.get(key)

    def put(self, key: str, value: str) -> None:
        with self.lock:
            self.done[key] = value
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")


CACHE = Cache(Path(__file__).parent / "data" / "taipei" / "llm_cache.jsonl")
# 連不上實驗室的模型時（轉接斷線、模型重開），等 30 秒再試，最多等 20 分鐘：期間把轉接重開就能接著跑
RETRY_WAIT, RETRIES = 30, 40


def chat(client: OpenAI, messages: list[dict], extra: dict, timeout: float) -> str:
    key = hashlib.sha256(json.dumps([messages, extra], ensure_ascii=False).encode()).hexdigest()
    if (cached := CACHE.get(key)) is not None:
        return cached
    for attempt in range(RETRIES):
        try:
            result = client.chat.completions.create(model="qwen3.5-9b", messages=messages, extra_body=extra,
                                                    timeout=timeout)
            break
        except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError):
            if attempt == RETRIES - 1:
                raise
            print(f"  （連不上實驗室的模型，{RETRY_WAIT} 秒後重試，第 {attempt + 1} 次）", flush=True)
            time.sleep(RETRY_WAIT)
    answer = (result.choices[0].message.content or "").strip()
    CACHE.put(key, answer)
    return answer


def parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    try:
        return json.loads(match.group()) if match else None
    except json.JSONDecodeError:
        return None
