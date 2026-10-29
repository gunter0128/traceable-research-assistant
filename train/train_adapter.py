# 訓練回答模型的 adapter（docs/decisions/040）
#
# 做什麼：底模 Qwen3.5-9B 凍結不動，只訓練外掛的 LoRA adapter，讓它照示範答案的寫法回答
#   一筆訓練資料 = 輸入（線上的回答原則 + 搜到的段落 + 題目）→ 輸出（合格的示範答案），由 train/make_answers.py 產生
#   格式跟線上一模一樣：Qwen 的對話格式、關掉思考模式；loss 只算回答的部分（輸入是給它讀的，不是要它學著寫）
# 怎麼分資料：每種題型各抽 10% 當驗證集，不拿來訓練
#   每訓練完一輪算一次驗證集的 loss，每一輪的 adapter 都存（adapters/taipei-50/epoch-1 …），記下 loss 最低的是哪一輪
#   剩下的 --n 50 就拿前 50 筆：50 筆 ⊂ 100 筆 ⊂ 全部，學習曲線比出來的才是資料量的差別
# 記錄：每次訓練的設定、資料的版本（檔案的 sha256）、loss 曲線、產出的 adapter 都記在 MLflow（實驗室本機的檔案）
#
# 用法（實驗室的訓練環境；要整張 GPU，先關掉兩個 vLLM）：
#   python train_adapter.py --smoke                       試跑：最長的 16 筆訓練 2 步，看記憶體夠不夠、一筆多久
#   python train_adapter.py --n 50                        訓練，adapter 存到 adapters/taipei-50
#   python train_adapter.py                               全部的訓練資料，存到 adapters/taipei-all
# 訓練完用 vLLM 直接掛上 adapter，確認過 vLLM 上的驗證集 loss 跟訓練時差不多（docs/decisions/040）

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path

import mlflow
import torch
import torch.nn.functional as F
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoTokenizer,
    Qwen3_5ForConditionalGeneration,
    get_linear_schedule_with_warmup,
)

BASE = "Qwen/Qwen3.5-9B"
SEED = 2026
VAL_EVERY = 10  # 每種題型抽 1/10 當驗證集
MAX_TOKENS = 4096
END = "<|im_end|>"  # Qwen 說完一段話的結束符號：要學會寫完就停
# 跟 Unsloth 的 Qwen3.5 範例一樣：完整注意力層（每 4 層 1 層）和每一層 MLP 的線性層
# 只掛在語言模型：看圖的部分用不到
TARGETS = r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
LORA = {"r": 16, "lora_alpha": 16, "lora_dropout": 0.0}
TRAIN = {"lr": 2e-4, "epochs": 3, "accumulation": 8, "warmup": 0.1}


# 照題型分組、組內隨機排：每組前 10% 當驗證集
# 剩下的照「在組內排第幾 ÷ 組的大小」排成一列：不管取前幾筆，每種題型的比例都跟全部差不多
def split(examples: list[dict]) -> tuple[list[dict], list[dict]]:
    rng = random.Random(SEED)
    groups = {}
    for ex in examples:
        groups.setdefault(ex["type"], []).append(ex)
    val, keyed = [], []
    for items in groups.values():
        rng.shuffle(items)
        k = round(len(items) / VAL_EVERY)
        val += items[:k]
        rest = items[k:]
        keyed += [((i + 0.5) / len(rest), rng.random(), ex) for i, ex in enumerate(rest)]
    train = [ex for *_, ex in sorted(keyed, key=lambda k: k[:2])]
    return train, val


# 輸入照線上的格式排好（Qwen 的對話格式、關掉思考模式，結尾是輪到模型回答），後面接示範答案和結束符號
# 輸入和答案分開斷字：線上 vLLM 也是只拿輸入去斷字，接著模型一個一個生出答案
def encode(tokenizer, ex: dict) -> dict:
    *prompt, answer = ex["messages"]
    text = tokenizer.apply_chat_template(prompt, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    prompt_ids = tokenizer(text, add_special_tokens=False).input_ids
    answer_ids = tokenizer(answer["content"] + END, add_special_tokens=False).input_ids
    return {"id": ex["id"], "ids": torch.tensor(prompt_ids + answer_ids), "answer_len": len(answer_ids)}


def load_model(path: str):
    # 看圖的部分（model.visual）用不到，放在 CPU，省下約 1GB 顯示卡記憶體；3090 只有 24GB
    return Qwen3_5ForConditionalGeneration.from_pretrained(
        path, dtype=torch.bfloat16, device_map={"model.visual": "cpu", "": 0})


# 只算回答的 loss：模型只算最後 answer_len + 1 個位置的預測（logits_to_keep），不用算整段 3,000 多個 token
#   字典有 24 萬多個字，整段都算會多用好幾 GB
# 位置 i 預測的是第 i+1 個 token：留下的最後一個位置在預測答案之後的東西，丟掉
def answer_loss(model, item: dict) -> torch.Tensor:
    ids = item["ids"].to("cuda")
    n = item["answer_len"]
    logits = model(input_ids=ids[None], logits_to_keep=n + 1, use_cache=False).logits[0, :-1]
    return F.cross_entropy(logits.float(), ids[-n:], reduction="sum")


# 驗證集上平均每個答案 token 的 loss
@torch.no_grad()
def val_loss(model, items: list[dict]) -> float:
    model.eval()
    total = sum(answer_loss(model, it).item() for it in items)
    model.train()
    return total / sum(it["answer_len"] for it in items)


def train(model, items: list[dict], val: list[dict], cfg: dict, out: Path) -> dict:
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=cfg["lr"], weight_decay=0.0)
    steps = math.ceil(len(items) / cfg["accumulation"]) * cfg["epochs"]
    # 學習率：前 10% 的步數從 0 慢慢升到 2e-4，之後直線降到 0
    scheduler = get_linear_schedule_with_warmup(optimizer, math.ceil(steps * cfg["warmup"]), steps)
    rng = random.Random(SEED)
    result = {"val_loss": [val_loss(model, val)], "best_epoch": 0}  # 第 0 輪 = 還沒訓練（adapter 一開始不改變輸出）
    mlflow.log_metric("val_loss", result["val_loss"][0], step=0)
    print(f"訓練前 驗證集 loss {result['val_loss'][0]:.4f}")
    step, seen, started = 0, 0, time.perf_counter()
    model.train()
    for epoch in range(1, cfg["epochs"] + 1):
        order = items[:]
        rng.shuffle(order)
        for start in range(0, len(order), cfg["accumulation"]):
            batch = order[start : start + cfg["accumulation"]]
            tokens = sum(it["answer_len"] for it in batch)
            total = 0.0
            # 一次只放得下 1 筆：8 筆各自算完、梯度累加起來才更新一次
            # 除以 8 筆答案的 token 總數：每個 token 的份量一樣，不會因為答案短就被放大
            for it in batch:
                loss = answer_loss(model, it) / tokens
                loss.backward()
                total += loss.item()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            step += 1
            seen += len(batch)
            mlflow.log_metrics({"train_loss": total, "lr": scheduler.get_last_lr()[0]}, step=step)
            print(f"  第 {epoch} 輪 第 {step}/{steps} 步 loss {total:.4f}")
        loss = val_loss(model, val)
        result["val_loss"].append(loss)
        mlflow.log_metric("val_loss", loss, step=step)
        print(f"第 {epoch} 輪 驗證集 loss {loss:.4f}")
        # 每輪都存：驗證集只有 17 筆，loss 最低的不一定考得最好，考試後可以直接換另一輪，不用重新訓練
        model.save_pretrained(out / f"epoch-{epoch}")  # 只存 adapter（約 100MB），不存底模
        if loss < min(result["val_loss"][:-1]):
            result["best_epoch"] = epoch
    result["seconds_per_example"] = round((time.perf_counter() - started) / seen, 1)
    # PyTorch 跟顯示卡要過的最大量（含暫時沒用到的），比實際用到的多一點，比較接近會不會爆
    result["peak_gb"] = round(torch.cuda.max_memory_reserved() / 2**30, 1)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", default="examples.jsonl")
    parser.add_argument("--n", type=int, help="用前幾筆訓練資料；不給就全部")
    parser.add_argument("--smoke", action="store_true", help="試跑：最長的 16 筆訓練 2 步")
    parser.add_argument("--name", default="taipei", help="adapter 名稱的開頭：第 2 版的資料用 taipei-v2，不蓋掉第 1 版")
    args = parser.parse_args()

    data = Path(args.examples).read_bytes()
    examples = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    train_ex, val_ex = split(examples)
    tokenizer = AutoTokenizer.from_pretrained(BASE)
    train_items = [encode(tokenizer, ex) for ex in train_ex[: args.n]]
    val_items = [encode(tokenizer, ex) for ex in val_ex]
    longest = max(len(it["ids"]) for it in train_items + val_items)
    if longest > MAX_TOKENS:
        raise SystemExit(f"最長的一筆有 {longest} 個 token，超過上限 {MAX_TOKENS}")
    cfg = dict(TRAIN)
    if args.smoke:
        train_items = sorted(train_items, key=lambda it: len(it["ids"]))[-16:]
        val_items = val_items[:2]
        cfg.update(epochs=1, warmup=0)
        tail = train_items[-1]
        print(f"最長的一筆 {len(tail['ids'])} 個 token；它的結尾（確認格式）：")
        print(tokenizer.decode(tail["ids"][-(tail["answer_len"] + 30) :]))

    model = load_model(BASE)
    # 用重算換記憶體：往前算時不留每層的中間結果，往回算時再算一次
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = get_peft_model(model, LoraConfig(target_modules=TARGETS, task_type="CAUSAL_LM", **LORA))
    model.print_trainable_parameters()

    name = "smoke" if args.smoke else f"{args.name}-{args.n or 'all'}"
    out = Path("adapters") / name
    mlflow.set_tracking_uri("sqlite:///mlflow.db")  # 記錄存在實驗室這台電腦，不送到外面
    mlflow.set_experiment("answer-adapter")
    with mlflow.start_run(run_name=name):
        mlflow.log_params({"base": BASE, "n_train": len(train_items), "n_val": len(val_items),
                           "data_sha256": hashlib.sha256(data).hexdigest()[:16], "max_tokens": longest,
                           "targets": TARGETS, **LORA, **cfg})
        result = train(model, train_items, val_items, cfg, out)
        result["train_ids"] = [it["id"] for it in train_items]
        result["val_ids"] = [it["id"] for it in val_items]
        out.mkdir(parents=True, exist_ok=True)
        (out / "train_info.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        mlflow.log_metrics({"best_val_loss": min(result["val_loss"]), "best_epoch": result["best_epoch"],
                            "seconds_per_example": result["seconds_per_example"], "peak_gb": result["peak_gb"]})
        mlflow.log_artifacts(str(out), artifact_path="adapter")
    print(f"最好的是第 {result['best_epoch']} 輪，驗證集 loss {min(result['val_loss']):.4f}；"
          f"一筆 {result['seconds_per_example']} 秒，顯示卡最多用 {result['peak_gb']} GB；存在 {out}")


if __name__ == "__main__":
    main()
