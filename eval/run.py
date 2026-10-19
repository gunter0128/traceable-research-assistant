# 用虛構的知識庫評估問答系統（B4，docs/decisions/017）
#
# 兩套知識庫：enterprise（青嶼科技，平常改進用）、school（澄溪國小，只拿來驗證改進能不能用到別的領域）
#
# 用法（專案資料夾）：
#   1. 開一個臨時資料庫（跟 CI 一樣，用完就丟）：
#        docker run -d --rm --name eval-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=eval -p 55433:5432 pgvector/pgvector:pg16
#   2. 產生 PDF（第一次才需要）：.venv\Scripts\python -m eval.build_pdfs --corpus enterprise
#   3. 跑評估：.venv\Scripts\python -m eval.run --corpus enterprise --runs 2 --note "基準"
#        企業題目預設只跑練習題（--split dev）；最後確認時才跑 --split test
#        學校題目整套都是考試題，預設全部跑
#   4. 用完關掉資料庫：docker stop eval-pg
#
# 流程：
#   建空的資料表 → 每份 PDF 走系統正常的處理流程（解析、切段、轉向量、存資料庫）
#   → 逐題呼叫 qa_service.ask（跟線上同一份程式碼）→ 評分 → 報表
# 評分：
#   檢索（程式判斷）：該引用的文件有沒有在系統搜到的段落裡
#   回答（gpt-4o 對清單）：一定要提到的有沒有提到、不能說的有沒有出現、該拒答的有沒有拒答
# 花費：企業 58 題每跑一輪，系統回答約 0.04 美元、評審約 0.3 美元；學校 21 題約三分之一

import os

# 一定要在 import app 之前換掉資料庫：.env 裡是正式資料庫，評估絕對不能碰它
os.environ["DATABASE_URL"] = os.environ.get(
    "EVAL_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55433/eval"
)

import argparse
import importlib
import json
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI
from sqlalchemy import text

# 載入所有資料表的定義，create_all 才建得出來
from app import models  # noqa: F401
from app.core import s3_client
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.repositories import document_repo, workspace_repo
from app.services import auth_service, document_service, qa_service
from eval.common import CORPORA, TYPE_NAMES

EVAL = Path(__file__).parent
RESULTS = EVAL / "data" / "results"
REPORT = EVAL.parent / "docs" / "eval-results.md"
JUDGE_MODEL = "gpt-4o"
TAIWAN = timezone(timedelta(hours=8))

def doc_code(name: str) -> str:
    # 「HR-02 第 6 條」或「HR-02_員工手冊_2026年版.pdf」→ HR-02；學校的是 S-01
    return re.match(r"[A-Z]+-\d+", name).group()


# ---------- 準備：建表、放進知識庫的文件 ----------
def setup_knowledge_base(corpus: str):
    # 本機模式：沒有 SQS，上傳後直接在同一個流程裡處理（docs/decisions/003）
    settings.sqs_queue_url = None
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    folder = EVAL / corpus
    pdfs = {pdf.name: pdf.read_bytes() for pdf in sorted((folder / "pdfs").glob("*.pdf"))}
    expected = len(list((folder / "docs").glob("*.md")))
    if len(pdfs) != expected:
        raise SystemExit(f"{corpus}/pdfs/ 裡應該有 {expected} 份 PDF，現在有 {len(pdfs)} 份。"
                         f"先跑 python -m eval.build_pdfs --corpus {corpus}")
    # 檔案不放 S3：處理時「從 S3 下載」改成直接讀本機的 PDF，其他步驟（解析、切段、轉向量、存資料庫）都跟線上一樣
    s3_client.download_file = lambda key: pdfs[key]

    db = SessionLocal()
    user = auth_service.register_user(db, "eval@example.com", "eval-password")
    workspace = workspace_repo.create(db, user.id, CORPORA[corpus]["workspace"])
    started = time.perf_counter()
    for name in pdfs:
        document = document_repo.create(db, workspace.id, name, name)
        document_service.process_document(db, document)
        if document.status != "processed":
            raise SystemExit(f"{name} 處理失敗：{document.error_message}")
    chunks = db.execute(text("SELECT count(*) FROM chunks")).scalar()
    print(f"知識庫：{len(pdfs)} 份文件、{chunks} 段，處理花了 {time.perf_counter() - started:.1f} 秒")
    return db, user, workspace, chunks


# ---------- 評審：gpt-4o 對清單 ----------
JUDGE_PROMPT = """你是問答系統的評審。系統回答的是{domain}的問題，你要逐項檢查系統的回答。

判斷原則：
- 看「意思」不看字面：同一件事換個說法也算有提到；數字必須一致（14 天和 15 天是不同的事）
- 「不能說的錯誤說法」：系統的回答裡如果有表達這個意思（包括當成答案或結論講出來），就算出現；
  如果系統只是在說明「那是舊版」「那不適用於你」時提到這個數字，不算出現
- 「拒答」：系統明確表示文件中找不到、沒有相關資訊、無法回答

問題：{question}

標準答案（參考用）：{answer}

系統的回答：
{response}

系統引用的文件：{cited}

請逐項判斷，輸出 JSON：
{{
  "must_include": [{{"item": "原文", "present": true/false, "reason": "一句話"}}],
  "must_not": [{{"item": "原文", "present": true/false, "reason": "一句話"}}],
  "refused": true/false
}}

一定要提到的：{must_include}
不能說的：{must_not}"""


def judge(client: OpenAI, domain: str, q: dict, response: str, cited: list[str]) -> dict:
    prompt = JUDGE_PROMPT.format(
        domain=domain, question=q["question"], answer=q["answer"], response=response, cited="、".join(cited) or "（無）",
        must_include=json.dumps(q["must_include"], ensure_ascii=False),
        must_not=json.dumps(q["must_not"], ensure_ascii=False),
    )
    result = client.chat.completions.create(
        model=JUDGE_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(result.choices[0].message.content)


# ---------- 一輪：逐題問、評分 ----------
def run_once(db, user, workspace, questions, client, domain, today) -> list[dict]:
    rows = []
    for q in questions:
        started = time.perf_counter()
        # 今天日期固定成知識庫設定的「現在」（2026-10-20），分數才不會因為哪天跑評估而變
        response = qa_service.ask(db, workspace.id, user.id, q["question"], today=today)
        seconds = time.perf_counter() - started
        # 系統目前把搜到的段落全部當引用回傳，所以「引用的文件」就是「搜到的文件」
        retrieved = sorted({doc_code(c.filename) for c in response.citations})
        required = sorted({doc_code(s) for s in q["sources"]})
        verdict = judge(client, domain, q, response.answer, retrieved)

        included = [v["present"] for v in verdict["must_include"]]
        violated = [v["present"] for v in verdict["must_not"]]
        refused = bool(verdict["refused"])
        row = {
            "id": q["id"], "type": q["type"], "question": q["question"], "response": response.answer,
            "seconds": round(seconds, 2), "retrieved": retrieved, "required": required,
            # 檢索完整（Full Hit）：該引用的文件全部都搜到了。沒有答案的題目不算
            "full_hit": None if q["should_refuse"] else all(r in retrieved for r in required),
            "include_rate": sum(included) / len(included) if included else None,
            "misuse": any(violated),
            "refused": refused,
            "refusal_ok": refused == q["should_refuse"],
            "verdict": verdict,
        }
        # 全對：重點全提到、沒有錯誤說法、拒答正確
        row["correct"] = row["refusal_ok"] and not row["misuse"] and (row["include_rate"] in (None, 1.0))
        rows.append(row)
        mark = "✓" if row["correct"] else "✗"
        print(f"  {mark} {q['id']} {q['question'][:30]:32} 檢索 {'完整' if row['full_hit'] else ('—' if row['full_hit'] is None else '缺')}"
              f"{'  誤用' if row['misuse'] else ''}{'  拒答' if refused else ''}")
    return rows


def summarize(rows: list[dict]) -> dict:
    def rate(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    def stats(group):
        answerable = [r for r in group if r["full_hit"] is not None]
        wrong = [r for r in answerable if not r["correct"]]
        return {
            "n": len(group),
            "correct": rate([r["correct"] for r in group]),
            "full_hit": rate([r["full_hit"] for r in group]),
            "misuse": rate([r["misuse"] for r in group]),
            "include": rate([r["include_rate"] for r in group]),
            "refusal_ok": rate([r["refusal_ok"] for r in group]),
            # 雙層分析（取自我的論文）：答錯的題目，是文件沒搜到（取得層），還是搜到了還答錯（使用層）
            "fail_acquisition": sum(1 for r in wrong if not r["full_hit"]),
            "fail_usage": sum(1 for r in wrong if r["full_hit"]),
        }

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    return {"all": stats(rows), **{t: stats(by_type[t]) for t in TYPE_NAMES if t in by_type}}


def pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def report_markdown(summaries: list[dict], meta: dict) -> str:
    lines = [f"## {meta['time']}　{meta['title']}　{meta['note']}", "",
             f"題目 {meta['split']}｜commit `{meta['commit']}`｜{meta['chunks']} 段｜搜尋前 {meta['top_k']} 段｜{len(summaries)} 輪｜評審 {JUDGE_MODEL}", ""]
    lines += ["| 題型 | 題數 | 全對 | 檢索完整 | 誤用 | 重點涵蓋 | 拒答正確 | 答錯：沒搜到 / 搜到還錯 |",
              "|---|---|---|---|---|---|---|---|"]
    for key in ["all", *TYPE_NAMES]:
        if key not in summaries[0]:
            continue
        cells = []
        for metric in ("correct", "full_hit", "misuse", "include", "refusal_ok"):
            cells.append(" / ".join(pct(s[key][metric]) for s in summaries))
        fails = " ； ".join(f"{s[key]['fail_acquisition']} / {s[key]['fail_usage']}" for s in summaries)
        name = "**全部**" if key == "all" else TYPE_NAMES[key]
        lines.append(f"| {name} | {summaries[0][key]['n']} | {' | '.join(cells)} | {fails} |")
    lines += ["", "跑了多輪時，每格依序是每一輪的結果（用來看隨機造成的波動）。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="同樣的設定跑幾輪（看分數的隨機波動）")
    parser.add_argument("--note", default="", help="這次評估的說明，例如「基準」「改 prompt」")
    parser.add_argument("--corpus", choices=CORPORA, required=True, help="enterprise 企業 / school 學校")
    parser.add_argument("--top-k", type=int,
                        help="每次搜幾段（不指定就用系統現在的設定 qa_service.SEARCH_LIMIT），用來比較不同段數")
    parser.add_argument("--split", choices=["dev", "test", "all"],
                        help="dev 練習題（平常改進用）/ test 考試題（只在最後確認時跑）/ all 全部；"
                             "不指定的話：有練習題就只跑練習題，沒有（學校）就全部跑")
    args = parser.parse_args()
    corpus = CORPORA[args.corpus]
    if args.top_k:
        qa_service.SEARCH_LIMIT = args.top_k

    lines = (EVAL / args.corpus / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    questions = [json.loads(line) for line in lines if line]
    split = args.split or ("dev" if any(q["split"] == "dev" for q in questions) else "all")
    if split != "all":
        questions = [q for q in questions if q["split"] == split]
    client = OpenAI(api_key=settings.openai_api_key)
    db, user, workspace, chunks = setup_knowledge_base(args.corpus)

    all_rows, summaries = [], []
    for i in range(args.runs):
        print(f"\n第 {i + 1} 輪")
        today = importlib.import_module(f"eval.{args.corpus}.rules").TODAY
        rows = run_once(db, user, workspace, questions, client, corpus["domain"], today)
        all_rows.append(rows)
        summaries.append(summarize(rows))

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    meta = {"time": datetime.now(TAIWAN).strftime("%Y-%m-%d %H:%M"), "note": args.note or "（無說明）",
            "corpus": args.corpus, "title": corpus["title"],
            "commit": commit, "chunks": chunks, "top_k": qa_service.SEARCH_LIMIT, "split": split}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{datetime.now(TAIWAN):%Y%m%d-%H%M}_{args.corpus}_{commit}.json"
    out.write_text(json.dumps({"meta": meta, "summaries": summaries, "runs": all_rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    markdown = report_markdown(summaries, meta)
    header = "# 評估結果\n\n每次評估的結果。題目和評分方式見 docs/decisions/016、017；逐題的明細在 eval/data/results/（不進 git）。\n\n"
    old = REPORT.read_text(encoding="utf-8").removeprefix(header) if REPORT.exists() else ""
    REPORT.write_text(header + markdown + "\n" + old, encoding="utf-8")
    print("\n" + markdown)
    print(f"逐題明細：{out}")
    db.close()


if __name__ == "__main__":
    main()
