# 寫示範答案並篩選：產生訓練資料（docs/decisions/039）
#
# 每一題：
#   1. 系統搜段落：跟真實使用一模一樣（本機的轉向量模型、只拿問題搜、找 5 段、照文件分組、附檔名和文件開頭）
#   2. 老師寫示範答案：Qwen 思考模式，回答原則加上理想回答的寫法（eval/run.py 的 IDEAL_RULES）
#   3. 篩選，全部通過才留下：
#      - 評審（Qwen 不思考，train/judge_check.py 量過跟 gpt-4o 一致 96%）對照標準答案：完全正確
#      - 有答案的題目至少標到一份出題時設定的文件；沒有答案的題目一個都不標
#      - 引用對得上：每句的數字都在它標的那段（eval/common.py citation_checks）
#      - 不矛盾（Qwen 不思考、思考各判一次，任何一個判矛盾就丟掉）
#      - 只用繁體中文、200 字以內
#   不合格的題目最多再寫 2 次（換隨機種子），有一次合格就留下；第一次跑只有 36% 合格（docs/decisions/039）
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
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import openai
import psycopg
from openai import OpenAI
from sqlalchemy.exc import ProgrammingError

from app.core import openai_client
from app.core.db import SessionLocal
from app.models.document import Document
from app.models.workspace import Workspace
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


# 上次建好的知識庫還在、文件數也對，就直接用：重建一次的話，轉向量的結果會有極小的差異，
# 少數題目搜到的段落不同，給老師的資料就跟上次不一樣，存起來的示範答案就用不上了
def existing_knowledge_base(expected: int):
    db = SessionLocal()
    try:
        workspace = db.query(Workspace).filter(Workspace.name == "臺北市政府規章").first()
        processed = db.query(Document).filter(Document.status == "processed").count() if workspace else 0
    except ProgrammingError:  # 第一次跑，還沒有資料表
        db.rollback()
        workspace, processed = None, 0
    if workspace is not None and processed == expected:
        print(f"沿用上次建好的知識庫：{processed} 份文件")
        return db, workspace
    db.close()
    return None, None


# 跟 qa_service.ask_with_passages 同一套步驟：搜段落、照文件分組、組成給模型看的資料
def retrieve(db, workspace_id: int, question: str) -> tuple[str, list[str], list[str]]:
    searches = [openai_client.PlannedSearch(question)]
    # 轉向量的服務也一樣：連不上就等一下再試（第二次跑時轉向量的模型停了，程式一開始就中斷）
    for attempt in range(RETRIES):
        try:
            embeddings = openai_client.embed_queries([question])
            break
        except openai_client.OpenAITemporaryError:
            if attempt == RETRIES - 1:
                raise
            print(f"  （連不上轉向量的模型，{RETRY_WAIT} 秒後重試，第 {attempt + 1} 次）", flush=True)
            time.sleep(RETRY_WAIT)
    chunks = qa_service._search(db, workspace_id, searches, embeddings, qa_service.SEARCH_LIMIT)
    groups = qa_service._group_by_document(chunks)
    headers = chunk_repo.first_chunks(db, [document.id for document, _ in groups])
    context = qa_service._build_context(groups, headers, TODAY)
    ordered = [(document, chunk) for document, group in groups for chunk in group]
    seen = [seen_passage(document.filename, headers.get(document.id, ""), chunk.content) for document, chunk in ordered]
    codes = [document.filename.split("_")[0] for document, _ in ordered]
    return context, seen, codes


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


CACHE = Cache(DATA / "llm_cache.jsonl")
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


def user_message(context: str, question: str) -> str:
    return f"資料：\n{context}\n\n問題：{question}\n\n{openai_client.CITATION_REMINDER}"


# 第一次跑時漏了一條：文件找不到答案時，老師會寫「找不到……[1][3][4]」，拿段落當沒有答案的證據（沒有答案的題目 42 題只 1 題合格）
# 重寫時（第 2 次起）加上這一條；第 1 次維持原本的規定，存好的示範答案才用得上
NO_ANSWER_RULE = "\n15. 文件裡找不到答案時，直接說「文件中找不到相關資訊」，並說明缺什麼；這時不要標段落編號。"


# 抽查 30 題發現的寫法問題：寫文件代號（「TP-079 已停辦」，161 題裡 16 題）、引用寫成 [第 5 條]
# 第 4 次起加上這一條；前幾次的規定不變，存好的示範答案才用得上
FORMAT_RULE = "\n16. 提到文件時寫文件名稱，不要寫文件代號（例如 TP-001）；段落編號只用 [數字]，例如 [2]，不要寫成 [第 5 條]。"


# attempt：第幾次寫（0 開始）。不合格的題目會再寫，每次換一個隨機種子，寫出不同的版本，有一次合格就留下
def teach(client: OpenAI, item: dict, attempt: int) -> str:
    rules = (openai_client.ANSWER_RULES + IDEAL_RULES + (NO_ANSWER_RULE if attempt >= 1 else "")
             + (FORMAT_RULE if attempt >= 3 else ""))
    messages = [{"role": "system", "content": rules},
                {"role": "user", "content": user_message(item["context"], item["q"]["question"])}]
    extra = QWEN_SAMPLING["think"] if attempt == 0 else {**QWEN_SAMPLING["think"], "seed": attempt}
    return chat(client, messages, extra, timeout=900)


NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0}

# 人工抽查判不合格的示範答案：{題號: {第幾次寫的}}，那幾次的答案丟掉，讓老師再寫
MANUAL_REJECT = {
    "t0132": {2},  # 「東、南大門上班時段 7 時至 8 時開放」看起來像只開 1 小時（應該是上午 7 時到晚間 8 時）
    # 問該交報告書還是提要表，沒說明甲類、乙類各是哪些出國，提問的人還是不知道要交哪一種
    # 第 2、3 次寫的也一樣：重點清單沒要求說明類別，評審擋不下來，整題不用
    "t0112": {1, 2, 3, 4},
    # 第 4 次寫的：沒說執行中不得轉借（只說「應持續使用於原定目的」），最後「暫無適用法規」會讓人誤會
    "t0046": {4},
}


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
    if contradicts(client, q["question"], answer, NO_THINK, detail, "consistency"):
        reasons.append("前後矛盾（不思考的評審）")
    if any(c in SIMPLIFIED for c in answer):
        reasons.append("有簡體字")
    if re.search(r"TP-\d+", answer):
        reasons.append("寫了文件代號")
    if re.search(r"[\[［](?!\d)[^\]］]{1,12}[\]］]", answer):
        reasons.append("引用不是 [數字] 的格式")
    if item["attempts"] in MANUAL_REJECT.get(q["id"], set()):
        reasons.append("人工抽查不合格")
    if len(answer) > MAX_CHARS:
        reasons.append(f"超過 {MAX_CHARS} 字")
    return not reasons, reasons, detail


# 不矛盾的評審：量過（train/judge_check.py --consistency），gpt-4o 判成矛盾的 19 個，不思考放過 7 個、思考放過 3 個
# 都不夠準，所以兩個都判，任何一個判矛盾就丟掉；思考的比較慢，只判其他檢查都通過的
def contradicts(client: OpenAI, question: str, answer: str, extra: dict, detail: dict, key: str) -> bool:
    verdict = parse_json(chat(client, [{"role": "user", "content": CONSISTENCY_PROMPT.format(
        question=question, response=answer)}], extra, timeout=900))
    detail[key] = verdict
    return verdict is None or bool(verdict.get("contradiction"))


# 寫一次、篩一次：結果記在每個 item 上（passed、reasons、detail、answer、cited、attempts）
def run_attempt(client: OpenAI, todo: list[dict], attempt: int) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda item: teach(client, item, attempt), todo))
    for item, answer in zip(todo, answers, strict=True):
        _, marked = qa_service._extract_citations(answer, len(item["seen"]))
        item.update(answer=answer, cited=sorted({item["codes"][n - 1] for n in marked}), attempts=attempt + 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: check(client, item), todo))
    # 其他檢查都通過的，再請思考模式判一次不矛盾
    survivors = [(item, result) for item, result in zip(todo, results, strict=True) if result[0]]

    def think_check(pair):
        item, (_, reasons, detail) = pair
        if contradicts(client, item["q"]["question"], item["answer"], QWEN_SAMPLING["think"], detail, "consistency_think"):
            reasons.append("前後矛盾（思考的評審）")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(think_check, survivors))
    for item, (_, reasons, detail) in zip(todo, results, strict=True):
        item.update(passed=not reasons, reasons=reasons, detail=detail)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=DATA / "questions_reviewed.jsonl")
    parser.add_argument("--attempts", type=int, default=4, help="不合格的題目最多寫幾次（含第一次）")
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    args = parser.parse_args()
    if not openai_client.embedding_client.base_url.host.startswith("localhost"):
        raise SystemExit("要先設好企業內部版的 EMBEDDING_* 環境變數：訓練資料要用本機的轉向量模型搜段落，跟上線時一樣")
    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line]

    create_database()
    pdfs = {pdf.name: pdf.read_bytes() for pdf in sorted((DATA / "pdfs").glob("*.pdf"))}
    db, workspace = existing_knowledge_base(len(pdfs))
    if workspace is None:
        db, _, workspace, _ = build_knowledge_base(pdfs, "臺北市政府規章")
    items = []
    for q in questions:
        context, seen, codes = retrieve(db, workspace.id, q["question"])
        items.append({"q": q, "context": context, "seen": seen, "codes": codes})
    db.close()

    client = OpenAI(base_url=args.base_url, api_key="not-needed", max_retries=2)
    for attempt in range(args.attempts):
        # 第 2 次起只重寫不合格的；有答案、但系統根本沒搜到出題那份規章的不重寫：段落裡沒有答案，再寫也不會對
        todo = [item for item in items if not item.get("passed") and (
            attempt == 0 or item["q"]["should_refuse"] or set(item["q"]["sources"]) & set(item["codes"]))]
        if not todo:
            break
        started = time.perf_counter()
        run_attempt(client, todo, attempt)
        print(f"第 {attempt + 1} 次：寫了 {len(todo)} 題，{(time.perf_counter() - started) / 60:.0f} 分鐘，"
              f"累計合格 {sum(bool(item.get('passed')) for item in items)} 題", flush=True)

    out, examples = [], []
    for item in items:
        q, passed, reasons, detail = item["q"], item["passed"], item["reasons"], item["detail"]
        out.append({"id": q["id"], "type": q["type"], "question": q["question"], "answer": item["answer"],
                    "cited": item["cited"], "retrieved": sorted(set(item["codes"])), "sources": q["sources"],
                    "attempts": item["attempts"], "passed": passed, "reasons": reasons, **detail})
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
