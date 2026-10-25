# 本機的 Qwen 能不能當篩選訓練資料的評審？跟 gpt-4o 的判斷比一致性（docs/decisions/039）
#
# 為什麼要量：老師（Qwen 思考模式）寫的示範答案約每 5 題錯 1 題，要靠評審把錯的丟掉
#   篩選一千多筆用 gpt-4o 超過預算，而且企業用機密文件做訓練資料時，檢查這一步也不能送到外部
#   本機的評審要是太寬鬆，錯的範例就會混進訓練資料，所以先量準不準
# 怎麼量：考卷上已經被 gpt-4o 評過的回答（評審規則、題目都已經是現在的版本），讓 Qwen 用一模一樣的評審規則再評一次
#   gpt-4o 的評審在 docs/decisions/030 抽查、修正過，當作標準
# 最要看「放錯率」：gpt-4o 判錯的回答裡，Qwen 判成全對的比例——這種會讓錯的範例混進訓練資料
# 事先訂的規則：放錯率 ≤ 10% 的模式裡，用最快的那個
#
# 用法（專案資料夾，實驗室的 Qwen 和轉接要開著）：
#   .venv\Scripts\python -m train.judge_check --mode no_think --n 300
#   .venv\Scripts\python -m train.judge_check --mode think --n 100

import argparse
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from eval.run import CONSISTENCY_PROMPT, JUDGE_PROMPT, QWEN_SAMPLING, _align

EVAL = Path(__file__).parent.parent / "eval"
OUT = Path(__file__).parent / "data" / "judge_check"
# 評審規則在 10/23 23:54 定稿、題目在 10/24 03:22 定稿，之後的評估結果才拿來比
SINCE = "20261024-0330"
DOMAINS = {"enterprise": "公司內部規章", "labor": "台灣的勞動法規", "school": "學校規章與家長須知"}


def load_questions() -> dict:
    questions = {}
    for corpus in DOMAINS:
        for line in (EVAL / corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines():
            if line:
                q = json.loads(line)
                questions[(corpus, q["id"])] = q
    return questions


# 照評分程式的算法，從評審的判斷算出「全對」
def correct_from(verdict: dict, q: dict) -> bool:
    included = _align(q["must_include"], verdict.get("must_include", []))
    violated = _align(q["must_not"], verdict.get("must_not", []))
    refusal_ok = bool(verdict.get("refused")) == q["should_refuse"]
    return refusal_ok and not any(violated) and (not included or all(included))


# 抽樣：gpt-4o 判全對、判錯的各一半（才量得準放錯率），每種回答的模型、知識庫都要有
def sample(questions: dict, n: int) -> list[dict]:
    pool = []
    for path in sorted((EVAL / "data" / "results").glob("2026*.json")):
        if path.stem < SINCE or path.stem.endswith(("_checks", "_rescored")) or "retrieval" in path.stem:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        corpus, model = data["meta"]["corpus"], data["meta"].get("model")
        for run in data.get("runs", []):
            for r in run:
                q = questions[(corpus, r["id"])]
                pool.append({"corpus": corpus, "model": model, "file": path.stem, "q": q, "response": r["response"],
                             "cited": r["cited"], "gpt_verdict": r["verdict"], "gpt_correct": correct_from(r["verdict"], q)})
    rng = random.Random(2026)
    rng.shuffle(pool)
    picked, seen = [], set()
    for want in (True, False):
        group = [p for p in pool if p["gpt_correct"] == want]
        # 同一題同一個回答只取一次；輪流從不同模型拿，避免都抽到同一種
        by_model = {}
        for p in group:
            key = (p["corpus"], p["q"]["id"], p["response"])
            if key not in seen:
                seen.add(key)
                by_model.setdefault(p["model"], []).append(p)
        chosen = []
        while len(chosen) < n // 2 and any(by_model.values()):
            for items in by_model.values():
                if items and len(chosen) < n // 2:
                    chosen.append(items.pop())
        picked += chosen
    return picked


def judge_local(client: OpenAI, mode: str, item: dict) -> tuple[dict | None, float]:
    q = item["q"]
    prompt = JUDGE_PROMPT.format(
        domain=DOMAINS[item["corpus"]], question=q["question"], answer=q["answer"], response=item["response"],
        cited="、".join(item["cited"]) or "（無）",
        must_include=json.dumps(q["must_include"], ensure_ascii=False),
        nice_to_have=json.dumps(q.get("nice_to_have", []), ensure_ascii=False),
        must_not=json.dumps(q["must_not"], ensure_ascii=False),
    )
    # 不思考：跟 gpt-4o 一樣 temperature 0，同一個回答每次判得一樣；思考：照 Qwen 建議的設定（temperature 0 容易一直重複）
    extra = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0} if mode == "no_think" else QWEN_SAMPLING["think"]
    started = time.perf_counter()
    result = client.chat.completions.create(model="qwen3.5-9b", messages=[{"role": "user", "content": prompt}],
                                            extra_body=extra, timeout=900)
    seconds = time.perf_counter() - started
    text = result.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)  # 模型偶爾會在 JSON 外面加說明或 ```json
    try:
        return json.loads(match.group()) if match else None, seconds
    except json.JSONDecodeError:
        return None, seconds


# 「不矛盾」的評審也一樣要先量：gpt-4o 補評過的回答（eval/trust_checks.py 的 *_checks.json），讓 Qwen 再判一次
# gpt-4o 判成矛盾的全部拿來（很少），判成不矛盾的隨機抽 100 個；最要看 gpt-4o 判矛盾、Qwen 卻判不矛盾的比例
# 量過：不思考放過 7/19，不夠準，所以也量思考模式
def check_consistency(client: OpenAI, mode: str) -> None:
    flagged, clean = [], []
    for checks in sorted((EVAL / "data" / "results").glob("*_checks.json")):
        rows = json.loads(checks.read_text(encoding="utf-8"))["rows"]
        source = json.loads(checks.with_name(checks.name.replace("_checks", "")).read_text(encoding="utf-8"))
        answers = {(i, r["id"]): r for i, run in enumerate(source["runs"]) for r in run}
        for c in rows:
            if "contradiction" in c:
                r = answers[(c["run"], c["id"])]
                (flagged if c["contradiction"] else clean).append(
                    {"question": r["question"], "response": r["response"], "gpt": c["contradiction"]})
    items = flagged + random.Random(2026).sample(clean, min(100, len(clean)))

    def judge(item):
        result = client.chat.completions.create(
            model="qwen3.5-9b", messages=[{"role": "user", "content": CONSISTENCY_PROMPT.format(
                question=item["question"], response=item["response"])}],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0} if mode == "no_think"
            else QWEN_SAMPLING["think"], timeout=900)
        match = re.search(r"\{.*\}", result.choices[0].message.content or "", flags=re.DOTALL)
        try:
            return bool(json.loads(match.group()).get("contradiction")) if match else None
        except json.JSONDecodeError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        verdicts = list(pool.map(judge, items))
    missed = sum(v is False for item, v in zip(items, verdicts, strict=True) if item["gpt"])
    extra = sum(v is True for item, v in zip(items, verdicts, strict=True) if not item["gpt"])
    unread = sum(v is None for v in verdicts)
    print(f"\n不矛盾的評審（Qwen {mode}）：gpt-4o 判矛盾 {len(flagged)} 個、判不矛盾抽 {len(items) - len(flagged)} 個，讀不出來 {unread} 個")
    print(f"  放過的矛盾（gpt-4o 判矛盾、Qwen 判不矛盾）：{missed}/{len(flagged)}")
    print(f"  多抓的（gpt-4o 判不矛盾、Qwen 判矛盾）：{extra}/{len(items) - len(flagged)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["no_think", "think"], default="no_think")
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--consistency", action="store_true", help="改量「不矛盾」的評審")
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    args = parser.parse_args()
    client = OpenAI(base_url=args.base_url, api_key="not-needed", max_retries=2)
    if args.consistency:
        check_consistency(client, args.mode)
        return
    items = sample(load_questions(), args.n)
    # 一次送 4 筆：實驗室的 vLLM 同時處理 4 筆（--max-num-seqs 4）
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: judge_local(client, args.mode, item), items))
    rows = []
    for item, (verdict, seconds) in zip(items, results, strict=True):
        rows.append({"corpus": item["corpus"], "id": item["q"]["id"], "model": item["model"], "seconds": round(seconds, 1),
                     "gpt_correct": item["gpt_correct"],
                     "qwen_correct": None if verdict is None else correct_from(verdict, item["q"]),
                     "gpt_verdict": item["gpt_verdict"], "qwen_verdict": verdict, "response": item["response"],
                     "question": item["q"]["question"]})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.mode}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    report(args.mode, rows)


def report(mode: str, rows: list[dict]) -> None:
    ok = [r for r in rows if r["qwen_correct"] is not None]
    wrong = [r for r in ok if not r["gpt_correct"]]
    right = [r for r in ok if r["gpt_correct"]]
    false_pass = sum(r["qwen_correct"] for r in wrong)
    false_fail = sum(not r["qwen_correct"] for r in right)
    print(f"\n評審模式：{mode}，{len(rows)} 個回答（gpt-4o 判全對 {len(right)}、判錯 {len(wrong)}），JSON 讀不出來 {len(rows) - len(ok)} 個")
    print(f"  全對的判斷一致：{sum(r['gpt_correct'] == r['qwen_correct'] for r in ok) / len(ok) * 100:.0f}%")
    print(f"  放錯率（gpt-4o 判錯、Qwen 判全對）：{false_pass}/{len(wrong)} = {false_pass / len(wrong) * 100:.0f}%")
    print(f"  錯殺率（gpt-4o 判全對、Qwen 判錯）：{false_fail}/{len(right)} = {false_fail / len(right) * 100:.0f}%")
    print(f"  每個回答平均 {sum(r['seconds'] for r in rows) / len(rows):.1f} 秒（同時送 4 筆）")


if __name__ == "__main__":
    main()
