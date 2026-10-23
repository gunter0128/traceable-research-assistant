# 用現在的評分規則和題目，重新評分之前存下來的回答（docs/decisions/030）
#
# 改了評分規則之後，舊的結果要用同一把尺重新評分才比得了。系統的回答都存在 eval/data/results/，
# 這裡只重新請評審評分，不重新回答：花的只有評審的錢，而且回答完全一樣，差別只來自評分規則
#
# 用法（專案資料夾）：
#   .venv\Scripts\python -m eval.rescore 20261023-1932_enterprise_2c97166 20261023-2209_enterprise_6d117d3 ...
# 每個檔案存一份重新評分的結果（檔名加上 _rescored），並在 docs/eval-results.md 最上面加一張新舊對照表

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from openai import OpenAI

# eval.run 一定要比 app 先 import：它一 import 就把資料庫換成臨時的。這裡不碰資料庫，但順序一樣比較保險
# isort: off
from eval.run import REPORT, RESULTS, TAIWAN, load_questions, pct, score, summarize
# isort: on

from app.core.config import settings
from eval.common import CORPORA

# 同時請評審評幾題：一題一題來要一個小時，評分彼此獨立，可以同時送
WORKERS = 8


def rescore_file(client: OpenAI, name: str) -> dict:
    data = json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))
    corpus = data["meta"]["corpus"]
    questions = {q["id"]: q for q in load_questions(corpus, "all")[0]}
    domain = CORPORA[corpus]["domain"]

    def one(row):
        return score(client, domain, questions[row["id"]], row["response"], row["retrieved"], row["cited"],
                     row["seconds"])

    with ThreadPoolExecutor(WORKERS) as pool:
        runs = [list(pool.map(one, run)) for run in data["runs"]]
    summaries = [summarize(rows) for rows in runs]
    meta = {**data["meta"], "rescored_at": datetime.now(TAIWAN).strftime("%Y-%m-%d %H:%M")}
    out = RESULTS / f"{name}_rescored.json"
    out.write_text(json.dumps({"meta": meta, "summaries": summaries, "runs": runs}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return {"name": name, "meta": data["meta"], "old": data["summaries"], "new": summaries}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="+", help="eval/data/results/ 裡的檔名（不含 .json）")
    args = parser.parse_args()
    client = OpenAI(api_key=settings.openai_api_key)

    results = []
    for name in args.names:
        print(f"\n{name}")
        results.append(rescore_file(client, name))

    def cells(summaries, metric):
        return " / ".join(pct(s["all"][metric]) for s in summaries)

    lines = [f"## {datetime.now(TAIWAN):%Y-%m-%d %H:%M}　重新評分：新的評分規則（docs/decisions/030）", "",
             "回答都是原本存下來的，只有評分規則和題目的加分項改了。每格依序是每一輪的結果。", "",
             "| 知識庫 | 題目 | 設定 | 全對（舊規則） | 全對（新規則） | 誤用（舊） | 誤用（新） |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        meta = r["meta"]
        setting = meta["note"]
        lines.append(f"| {meta['title']} | {meta['split']} | {setting} | {cells(r['old'], 'correct')} | "
                     f"{cells(r['new'], 'correct')} | {cells(r['old'], 'misuse')} | {cells(r['new'], 'misuse')} |")
    markdown = "\n".join(lines) + "\n"
    header = "# 評估結果\n\n每次評估的結果。題目和評分方式見 docs/decisions/016、017；逐題的明細在 eval/data/results/（不進 git）。\n\n"
    old = REPORT.read_text(encoding="utf-8").removeprefix(header) if REPORT.exists() else ""
    REPORT.write_text(header + markdown + "\n" + old, encoding="utf-8")
    print("\n" + markdown)


if __name__ == "__main__":
    main()
