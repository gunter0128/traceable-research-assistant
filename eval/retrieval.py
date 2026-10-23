# 只比檢索：不同的規劃方式、不同的段數，該用的文件有沒有搜到（docs/decisions/028）
#
# 跟 eval/run.py 的差別：不生成答案、不請評審，只看「搜到哪些文件」，所以很便宜、很快
# 用來先篩掉明顯不好的組合，剩下的再用 eval/run.py 跑完整的評估（答案對不對）
#
# 用法（專案資料夾，臨時資料庫要先開好，見 eval/run.py）：
#   .venv\Scripts\python -m eval.retrieval --corpus enterprise
#   預設比 none / rewrite / checklist / catalog 四種規劃 × 5、8 段，只用練習題
#
# 同一題的規劃只做一次，再分別取 5 段、8 段：段數不同的結果，差別只來自段數，不會混到規劃的隨機性

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

# eval.run 一定要比 app 先 import：它一 import 就把資料庫換成臨時的，順序反過來會連到正式資料庫
# 下面的 isort: off / on 是叫排序工具不要把它排到 app 後面
# isort: off
from eval.run import (REPORT, RESULTS, TAIWAN, current_commit, doc_code, load_evidence, load_questions,
                      setup_knowledge_base)
# isort: on

from app.core import openai_client
from app.services import qa_service
from eval.common import CORPORA, TYPE_NAMES, evidence_rate

PLANNERS = ["none", "rewrite", "checklist", "catalog"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, required=True)
    parser.add_argument("--split", choices=["dev", "test", "all"], help="不指定的話跟 eval/run.py 一樣")
    parser.add_argument("--planners", nargs="+", choices=PLANNERS, default=PLANNERS)
    parser.add_argument("--top-k", nargs="+", type=int, default=[5, 8], help="合併之後留幾段")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    questions, split = load_questions(args.corpus, args.split)
    evidence = load_evidence(args.corpus)
    # 沒有答案的題目沒有「該用的文件」，不算檢索
    questions = [q for q in questions if not q["should_refuse"]]
    db, _, workspace, chunks = setup_knowledge_base(args.corpus)

    rows = []
    for planner in args.planners:
        qa_service.PLANNER = planner
        print(f"\n規劃：{planner}")
        for q in questions:
            usages = []
            started = time.perf_counter()
            deadline = time.monotonic() + qa_service.ANSWER_BUDGET_SECONDS
            planned = qa_service._plan(db, workspace.id, q["question"], deadline, usages.append)
            plan_seconds = time.perf_counter() - started
            searches = [openai_client.PlannedSearch(q["question"]), *planned]
            embeddings = openai_client.embed_queries([s.query for s in searches])
            required = sorted({doc_code(s) for s in q["sources"]})
            for k in args.top_k:
                merged = qa_service._search(db, workspace.id, searches, embeddings, k)
                retrieved = sorted({doc_code(chunk.document.filename) for chunk in merged})
                found = [r for r in required if r in retrieved]
                quotes = evidence.get(q["id"], [])
                quote_rate = evidence_rate(quotes, [chunk.content for chunk in merged])
                rows.append({
                    "planner": planner, "top_k": k, "id": q["id"], "type": q["type"], "question": q["question"],
                    "queries": [vars(s) for s in searches], "retrieved": retrieved, "required": required,
                    "full_hit": len(found) == len(required), "recall": len(found) / len(required),
                    "evidence_rate": quote_rate, "evidence_full": None if quote_rate is None else quote_rate == 1,
                    "plan_seconds": round(plan_seconds, 2),
                    "plan_cost": float(sum((u.cost_usd() for u in usages), Decimal(0))),
                })
            mark = "✓" if rows[-1]["full_hit"] else "✗"
            plan_text = " ｜ ".join(f"{s.query}" + (f"＠{s.document}" if s.document else "") for s in planned)
            print(f"  {mark} {q['id']} {q['question'][:28]:30} {plan_text[:100]}")

    meta = {"time": datetime.now(TAIWAN).strftime("%Y-%m-%d %H:%M"), "corpus": args.corpus,
            "title": CORPORA[args.corpus]["title"], "split": split, "commit": current_commit(), "chunks": chunks,
            "questions": len(questions), "note": args.note or "只比檢索：規劃方式 × 段數"}
    save(meta, rows, args.planners, args.top_k)
    db.close()


def summarize(rows: list[dict]) -> dict:
    def mean(values):
        return sum(values) / len(values) if values else None

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    return {
        "full_hit": mean([r["full_hit"] for r in rows]),
        "recall": mean([r["recall"] for r in rows]),
        "evidence_full": mean([r["evidence_full"] for r in rows if r["evidence_full"] is not None]),
        "queries": mean([len(r["queries"]) for r in rows]),
        "plan_seconds": mean([r["plan_seconds"] for r in rows]),
        "plan_cost": sum(r["plan_cost"] for r in rows),
        "by_type": {t: mean([r["full_hit"] for r in by_type[t]]) for t in TYPE_NAMES if t in by_type},
    }


def save(meta: dict, rows: list[dict], planners: list[str], top_ks: list[int]) -> None:
    summaries = {f"{p}@{k}": summarize([r for r in rows if r["planner"] == p and r["top_k"] == k])
                 for p in planners for k in top_ks}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now(TAIWAN):%Y%m%d-%H%M}_{meta['corpus']}_retrieval.json"
    out.write_text(json.dumps({"meta": meta, "summaries": summaries, "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    types = list(next(iter(summaries.values()))["by_type"])
    with_evidence = next(iter(summaries.values()))["evidence_full"] is not None
    lines = [f"## {meta['time']}　{meta['title']}　{meta['note']}", "",
             f"題目 {meta['split']}（有答案的 {meta['questions']} 題）｜commit `{meta['commit']}`｜{meta['chunks']} 段｜只看檢索，不生成答案", "",
             "| 規劃 | 段數 | 檢索完整 | 文件找回率 | " + ("原文全部找到 | " if with_evidence else "")
             + " | ".join(TYPE_NAMES[t] for t in types) + " | 平均幾句 | 規劃秒數 | 規劃花費 |",
             "|---|---|---|---|" + ("---|" if with_evidence else "") + "---|" * len(types) + "---|---|---|"]
    for key, s in summaries.items():
        planner, k = key.split("@")
        cells = [f"{s['full_hit'] * 100:.0f}%", f"{s['recall'] * 100:.0f}%",
                 *([f"{s['evidence_full'] * 100:.0f}%"] if with_evidence else []),
                 *(f"{s['by_type'][t] * 100:.0f}%" for t in types),
                 f"{s['queries']:.1f}", f"{s['plan_seconds']:.1f}", f"${s['plan_cost']:.4f}"]
        lines.append(f"| {planner} | {k} | " + " | ".join(cells) + " |")
    lines += ["", "文件找回率：該用的文件平均找到幾成（檢索完整要全部找到才算）。"
              + ("原文全部找到：答案依據的條文原文都在找到的段落裡（docs/decisions/031）。" if with_evidence else ""), ""]
    markdown = "\n".join(lines)

    header = "# 評估結果\n\n每次評估的結果。題目和評分方式見 docs/decisions/016、017；逐題的明細在 eval/data/results/（不進 git）。\n\n"
    old = REPORT.read_text(encoding="utf-8").removeprefix(header) if REPORT.exists() else ""
    REPORT.write_text(header + markdown + "\n" + old, encoding="utf-8")
    print("\n" + markdown)
    print(f"逐題明細：{out}")


if __name__ == "__main__":
    main()
