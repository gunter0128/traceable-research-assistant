# 寫示範答案並篩選：產生訓練資料（docs/decisions/039）
#
# 每一題：
#   1. 系統搜段落：跟真實使用一模一樣（本機的轉向量模型、只拿問題搜、找 5 段、照文件分組、附檔名和文件開頭）
#   2. 老師寫示範答案：Qwen 思考模式，回答原則加上理想回答的寫法（eval/run.py 的 IDEAL_RULES）
#   3. 篩選，全部通過才留下：
#      - 評審（Qwen 不思考，train/judge_check.py 量過跟 gpt-4o 一致 96%）對照標準答案：完全正確
#      - 有答案的題目至少標到一份出題時設定的文件；沒有答案的題目一個都不標
#      - 引用對得上：每句的數字都在它標的那段（eval/common.py citation_checks）
#      - 不矛盾（Qwen 不思考，同一套不矛盾的評審規則）
#      - 只用繁體中文、200 字以內
#   4. 一筆訓練資料 = 輸入（線上的回答原則，不含理想回答的寫法 + 搜到的段落 + 題目）→ 輸出（合格的示範答案）
#      訓練時不給理想回答的寫法：模型要自己學會這些習慣
#
# 用法（專案資料夾；實驗室的兩個模型和轉接要開著；臨時資料庫要開著）：
#   設好企業內部版的 EMBEDDING_* 環境變數（docs/decisions/037），再跑
#   .venv\Scripts\python -m train.make_answers
# 結果在 train/data/taipei/answers.jsonl（每題的過程和篩選結果）、examples.jsonl（合格的訓練資料）

import os

# 一定要在 import app 之前換掉資料庫：訓練資料用自己的臨時資料庫，不跟評估的混在一起
os.environ["EVAL_DATABASE_URL"] = os.environ.get("TRAIN_DATABASE_URL", "postgresql+psycopg://postgres:test@localhost:55433/train")

# isort: off
from eval.run import CONSISTENCY_PROMPT, IDEAL_RULES, JUDGE_PROMPT, QWEN_SAMPLING, build_knowledge_base, seen_passage
# isort: on

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import psycopg
from openai import OpenAI

from app.core import openai_client
from app.repositories import chunk_repo
from app.services import qa_service
from eval.common import citation_checks
from train.judge_check import correct_from
from train.make_questions import SIMPLIFIED

DATA = Path(__file__).parent / "data" / "taipei"
TODAY = date(2026, 10, 25)  # 規章是這天下載的，「今天」固定成這天
MAX_CHARS = 200
DOMAIN = "臺北市政府的內部規章"


def create_database() -> None:
    url = os.environ["EVAL_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    admin, name = url.rsplit("/", 1)
    with psycopg.connect(f"{admin}/postgres", autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')


# 跟 qa_service.ask_with_passages 同一套步驟：搜段落、照文件分組、組成給模型看的資料
def retrieve(db, workspace_id: int, question: str) -> tuple[str, list[str], list[str]]:
    searches = [openai_client.PlannedSearch(question)]
    embeddings = openai_client.embed_queries([question])
    chunks = qa_service._search(db, workspace_id, searches, embeddings, qa_service.SEARCH_LIMIT)
    groups = qa_service._group_by_document(chunks)
    headers = chunk_repo.first_chunks(db, [document.id for document, _ in groups])
    context = qa_service._build_context(groups, headers, TODAY)
    ordered = [(document, chunk) for document, group in groups for chunk in group]
    seen = [seen_passage(document.filename, headers.get(document.id, ""), chunk.content) for document, chunk in ordered]
    codes = [document.filename.split("_")[0] for document, _ in ordered]
    return context, seen, codes


def chat(client: OpenAI, messages: list[dict], extra: dict, timeout: float) -> str:
    result = client.chat.completions.create(model="qwen3.5-9b", messages=messages, extra_body=extra, timeout=timeout)
    return (result.choices[0].message.content or "").strip()


def parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    try:
        return json.loads(match.group()) if match else None
    except json.JSONDecodeError:
        return None


def user_message(context: str, question: str) -> str:
    return f"資料：\n{context}\n\n問題：{question}\n\n{openai_client.CITATION_REMINDER}"


def teach(client: OpenAI, item: dict) -> str:
    messages = [{"role": "system", "content": openai_client.ANSWER_RULES + IDEAL_RULES},
                {"role": "user", "content": user_message(item["context"], item["q"]["question"])}]
    return chat(client, messages, QWEN_SAMPLING["think"], timeout=900)


NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0}


# 篩選：回傳 (合格與否, 不合格的原因, 細節)
def check(client: OpenAI, item: dict) -> tuple[bool, list[str], dict]:
    q, answer = item["q"], item["answer"]
    reasons, detail = [], {}
    if not answer:
        return False, ["沒有答案（想到長度上限）"], detail
    prompt = JUDGE_PROMPT.format(
        domain=DOMAIN, question=q["question"], answer=q["answer"], response=answer, cited="、".join(item["cited"]) or "（無）",
        must_include=json.dumps(q["must_include"], ensure_ascii=False), nice_to_have="[]",
        must_not=json.dumps(q["must_not"], ensure_ascii=False))
    verdict = parse_json(chat(client, [{"role": "user", "content": prompt}], NO_THINK, timeout=300))
    detail["verdict"] = verdict
    if verdict is None or not correct_from(verdict, q):
        reasons.append("評審判不完全正確")
    if q["should_refuse"]:
        if item["cited"]:
            reasons.append("沒有答案的題目卻標了出處")
    elif not set(item["cited"]) & set(q["sources"]):
        reasons.append("沒有標到出題時設定的文件")
    checks = citation_checks(answer, item["seen"], f"{q['question']}\n今天日期：{qa_service._date_text(TODAY)}")
    detail["citation_checks"] = checks
    if any(c["missing"] for c in checks):
        reasons.append("引用對不上")
    consistency = parse_json(chat(client, [{"role": "user", "content": CONSISTENCY_PROMPT.format(
        question=q["question"], response=answer)}], NO_THINK, timeout=300))
    detail["consistency"] = consistency
    if consistency is None or consistency.get("contradiction"):
        reasons.append("前後矛盾")
    if any(c in SIMPLIFIED for c in answer):
        reasons.append("有簡體字")
    if len(answer) > MAX_CHARS:
        reasons.append(f"超過 {MAX_CHARS} 字")
    return not reasons, reasons, detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=DATA / "questions_reviewed.jsonl")
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    args = parser.parse_args()
    if not openai_client.embedding_client.base_url.host.startswith("localhost"):
        raise SystemExit("要先設好企業內部版的 EMBEDDING_* 環境變數：訓練資料要用本機的轉向量模型搜段落，跟上線時一樣")
    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line]

    create_database()
    pdfs = {pdf.name: pdf.read_bytes() for pdf in sorted((DATA / "pdfs").glob("*.pdf"))}
    db, _, workspace, _ = build_knowledge_base(pdfs, "臺北市政府規章")
    items = []
    for q in questions:
        context, seen, codes = retrieve(db, workspace.id, q["question"])
        items.append({"q": q, "context": context, "seen": seen, "codes": codes})
    db.close()

    client = OpenAI(base_url=args.base_url, api_key="not-needed", max_retries=2)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda item: teach(client, item), items))
    print(f"示範答案寫完：{len(items)} 題，{(time.perf_counter() - started) / 60:.0f} 分鐘")
    for item, answer in zip(items, answers, strict=True):
        _, marked = qa_service._extract_citations(answer, len(item["seen"]))
        item.update(answer=answer, cited=sorted({item["codes"][n - 1] for n in marked}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: check(client, item), items))

    out, examples = [], []
    for item, (passed, reasons, detail) in zip(items, results, strict=True):
        q = item["q"]
        out.append({"id": q["id"], "type": q["type"], "question": q["question"], "answer": item["answer"],
                    "cited": item["cited"], "retrieved": sorted(set(item["codes"])), "sources": q["sources"],
                    "passed": passed, "reasons": reasons, **detail})
        if passed:
            # 訓練時給的是線上的回答原則（不含理想回答的寫法），跟上線時模型看到的一模一樣
            examples.append({"id": q["id"], "type": q["type"], "messages": [
                {"role": "system", "content": openai_client.ANSWER_RULES},
                {"role": "user", "content": user_message(item["context"], q["question"])},
                {"role": "assistant", "content": item["answer"]}]})
    (DATA / "answers.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    (DATA / "examples.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in examples),
                                        encoding="utf-8")
    print(f"合格 {len(examples)} / {len(out)}（{len(examples) / len(out) * 100:.0f}%）")
    for reason in sorted({r for row in out for r in row["reasons"]}):
        print(f"  {reason}：{sum(reason in row['reasons'] for row in out)}")


if __name__ == "__main__":
    main()
