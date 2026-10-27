# 用 vLLM 算驗證集的 loss，確認 vLLM 上跑的模型跟訓練時是同一個（docs/decisions/040）
#
# 為什麼要：有人回報 vLLM 直接掛 adapter 在 Qwen3.5 上不會生效（不報錯，但回答跟沒掛一樣）
#   這裡直接量：有生效的話 loss 會跟訓練時差不多，沒生效會跟沒訓練的一樣
#   vLLM 跟訓練程式的算法有一點不同，所以要連沒訓練的模型一起量，各自跟自己的「沒訓練」比
# 怎麼算：把輸入 + 示範答案整段送給 vLLM，請它回傳每個 token 的 log 機率（prompt_logprobs），只取答案的部分算平均
#   斷字跟訓練時一樣（train_adapter.encode），直接送 token 編號，不讓 vLLM 自己斷字
#
# 用法（訓練環境；vLLM 要先開好）：
#   python vllm_loss.py --smoke qwen3.5-9b smoke    試跑的 2 筆驗證集，依序算每個模型名稱（vLLM 的 --served-model-name 或 adapter 名稱）
#   python vllm_loss.py qwen3.5-9b taipei-50        全部 17 筆驗證集

import argparse
import json
import urllib.request
from pathlib import Path

from train_adapter import BASE, encode, split
from transformers import AutoTokenizer

URL = "http://localhost:8000/v1/completions"


# 回傳答案部分的 loss 總和：每個 token 的 log 機率取負的加起來，跟訓練時的 answer_loss 一樣
# vLLM 回傳的第 i 個位置，是看了前面 i 個 token 之後，第 i 個 token 的 log 機率；答案是最後 answer_len 個
def answer_loss(model: str, item: dict) -> float:
    ids = item["ids"].tolist()
    body = {"model": model, "prompt": ids, "max_tokens": 1, "prompt_logprobs": 0}
    request = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        logprobs = json.load(response)["choices"][0]["prompt_logprobs"]
    return -sum(logprobs[i][str(ids[i])]["logprob"] for i in range(len(ids) - item["answer_len"], len(ids)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="+", help="要比較的模型名稱")
    parser.add_argument("--examples", default="examples.jsonl")
    parser.add_argument("--smoke", action="store_true", help="只用試跑的 2 筆驗證集")
    args = parser.parse_args()

    lines = Path(args.examples).read_text(encoding="utf-8").splitlines()
    _, val_ex = split([json.loads(line) for line in lines if line])
    tokenizer = AutoTokenizer.from_pretrained(BASE)
    items = [encode(tokenizer, ex) for ex in val_ex]
    if args.smoke:
        items = items[:2]
    tokens = sum(it["answer_len"] for it in items)
    for model in args.models:
        print(f"{model} 驗證集 loss {sum(answer_loss(model, it) for it in items) / tokens:.4f}")


if __name__ == "__main__":
    main()
