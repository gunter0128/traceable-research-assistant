# 寫示範答案並篩選：產生訓練資料（docs/decisions/039）
#
# 每一題：
#   1. 系統搜段落：跟真實使用一模一樣（本機的轉向量模型、只拿問題搜、找 5 段、照文件分組、附檔名和文件開頭）
#   2. 老師寫示範答案：Qwen 思考模式，回答原則加上理想回答的寫法（IDEAL_RULES）和下面的第 15～20 條
#   3. 篩選，全部通過才留下：
#      - 評審（Qwen 不思考，量過跟 gpt-4o 一致 96%，docs/decisions/039）對照標準答案：完全正確
#      - 有答案的題目至少標到一份出題時設定的文件；沒有答案的題目一個都不標
#      - 引用對得上：每句的數字都在它標的那段（eval/common.py citation_checks）
#      - 有答案的題目：依據那一行之前每一句都標了段落編號、依據另起一行（第 2 版，041）
#      - 標的編號都是段落編號：老師有時把條次寫成 [7]，資料只有 [1]～[5]（第 2 版，041）
#      - 不矛盾（Qwen 不思考、思考各判一次，任何一個判矛盾就丟掉）
#      - 只用繁體中文、200 字以內
#   不合格的題目最多再寫 3 次（換隨機種子），有一次合格就留下；第 1 版第一次跑只有 36% 合格（docs/decisions/039）
#   段落裡沒有出題時記下的原文的不重寫：老師只能說資料沒寫，再寫也不會對（第 2 版，041）
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
from eval.run import CONSISTENCY_PROMPT, JUDGE_PROMPT, QWEN_SAMPLING, _align, build_knowledge_base, seen_passage
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
from sqlalchemy.exc import ProgrammingError

from app.core import openai_client
from app.core.db import SessionLocal
from app.models.document import Document
from app.models.workspace import Workspace
from app.repositories import chunk_repo
from app.services import qa_service
from eval.common import citation_checks
from train.llm import RETRIES, RETRY_WAIT, chat, parse_json
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
    # 轉向量的服務也一樣：連不上就等一下再試（第二次跑時轉向量的模型停了，程式一開始就中斷）
    for attempt in range(RETRIES):
        try:
            [embedding] = openai_client.embed_queries([question])
            break
        except openai_client.OpenAITemporaryError:
            if attempt == RETRIES - 1:
                raise
            print(f"  （連不上轉向量的模型，{RETRY_WAIT} 秒後重試，第 {attempt + 1} 次）", flush=True)
            time.sleep(RETRY_WAIT)
    chunks = chunk_repo.search_similar(db, workspace_id, embedding, limit=qa_service.SEARCH_LIMIT)
    # 相連的段落合併，跟線上一樣（docs/decisions/043；第 2 版以前的資料是沒合併的）
    groups = qa_service._merge_adjacent(qa_service._group_by_document(chunks))
    headers = chunk_repo.first_chunks(db, [document.id for document, _ in groups])
    context = qa_service._build_context(groups, headers, TODAY)
    ordered = [(document, chunk) for document, group in groups for chunk in group]
    seen = [seen_passage(document.filename, headers.get(document.id, ""), chunk.content) for document, chunk in ordered]
    codes = [document.filename.split("_")[0] for document, _ in ordered]
    return context, seen, codes


# 段落裡有沒有出題時記下的原文（evidence）：原文每 8 個字一組，四分之一以上找得到就算有（換行、空白先拿掉）
# 第 2 版（041）：搜到出題那份規章的有 98%，但搜到寫著答案那一段的只有 87%（一半以上找得到才算）
# 這裡用四分之一，寧可多寫幾次：一半的話，答案那段其實有搜到、只是跟原文差幾個字的 t0043、t0061 也會被當成沒搜到
def evidence_retrieved(item: dict) -> bool:
    context = re.sub(r"\s+", "", item["context"])

    def found(evidence: str) -> bool:
        text = re.sub(r"\s+", "", evidence)
        grams = [text[i:i + 8] for i in range(max(1, len(text) - 7))]
        return sum(gram in context for gram in grams) / len(grams) >= 0.25

    return not item["q"].get("evidence") or any(found(e) for e in item["q"]["evidence"])


# 理想回答的寫法（docs/decisions/038），接在回答原則第 7 條後面，只給老師看：學生（上線的模型）從示範答案學這些習慣
# 跟回答原則一樣只放通用的寫法，不放任何領域的詞（docs/decisions/019）
IDEAL_RULES = """
8. 第一句直接給結論，不要先鋪陳背景或重述問題。
9. 用自己的話整理重點，不要整段照抄資料的原文；同一件事只講一次。
10. 結論只有一個：不要寫到一半改口或更正自己，前後的數字要一致。
11. 只詳細說明適用於提問者的規定；不適用的（舊版、草案、還沒生效、別的單位），用一句話說明為什麼不適用，不要列出它的內容。
12. 問題沒說明對象或情況、答案又因此不同時，每種情況各用一句話說明。
13. 每一句標的段落編號，那一段裡要真的寫著這句話講的內容。
14. 整個回答盡量在 150 字以內，需要分情況時可以稍長；不要加「結論」「說明」這類標題。"""


def user_message(context: str, question: str) -> str:
    return f"資料：\n{context}\n\n問題：{question}\n\n{openai_client.CITATION_REMINDER}"


# 第 1 版漏了一條：文件找不到答案時，老師會寫「找不到……[1][3][4]」，拿段落當沒有答案的證據（沒有答案的題目 42 題只 1 題合格）
NO_ANSWER_RULE = "\n15. 文件裡找不到答案時，直接說「文件中找不到相關資訊」，並說明缺什麼；這時不要標段落編號。"


# 第 1 版抽查 30 題發現的寫法問題：寫文件代號（「TP-079 已停辦」，161 題裡 16 題）、引用寫成 [第 5 條]
FORMAT_RULE = "\n16. 提到文件時寫文件名稱，不要寫文件代號（例如 TP-001）；段落編號只用 [數字]，例如 [2]，不要寫成 [第 5 條]。"


# 第 2 版（docs/decisions/041）：第 1 版訓練出來的模型考練習題時，選錯適用的規定、漏了限制和例外、
# 說明不適用的句子沒標引用、依據那一行時有時無。原本的規定大多有寫，只是不夠精準；講清楚之後放在老師的規定裡，
# 學生從示範答案學（上線時給學生的回答原則不變，跟訓練時一樣）
# 第 17 條：上線時直接加「條件不符合就不能當答案」，模型只要規定沒直接寫到提問者就說找不到
#   （不該說找不到的從 3 題變 9 題），所以改成先找有沒有比照、準用的規定
TEACHER_RULES = """
17. 下結論前，先找出規定寫的適用條件（對象、身分、單位、職級、期間、生效或廢止日期），對照提問者的情況和今天的日期。規定沒有直接寫到提問者時，先找資料裡有沒有寫他比照、準用或適用哪一條；確定都沒有，才說缺什麼。
18. 規定裡有限制或例外時（例如某些人不適用、不給薪、有上限），也要一起說。
19. 每一句講到資料內容的句子都要標段落編號，包括結論、說明某條規定不適用、已廢止、還沒生效的句子；只有最後的依據那一行不用標。
20. 回答的最後另起一行，寫「依據：文件名稱 條次」；說找不到相關資訊時不用寫。"""


# attempt：第幾次寫（0 開始）。不合格的題目會再寫，每次換一個隨機種子，寫出不同的版本，有一次合格就留下
# 第 1 版第 15、16 條是第 2、4 次寫起才加（為了用上存好的答案）；第 2 版舊題目也全部重寫，所有規定從第 1 次就給
def teach(client: OpenAI, item: dict, attempt: int) -> str:
    rules = openai_client.ANSWER_RULES + IDEAL_RULES + NO_ANSWER_RULE + FORMAT_RULE + TEACHER_RULES
    messages = [{"role": "system", "content": rules},
                {"role": "user", "content": user_message(item["context"], item["q"]["question"])}]
    extra = QWEN_SAMPLING["think"] if attempt == 0 else {**QWEN_SAMPLING["think"], "seed": attempt}
    return chat(client, messages, extra, timeout=900)


NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0}


# 照評分程式的算法，從評審的判斷算出「全對」（跟 eval/run.py 的 score 一樣）
def correct_from(verdict: dict, q: dict) -> bool:
    included = _align(q["must_include"], verdict.get("must_include", []))
    violated = _align(q["must_not"], verdict.get("must_not", []))
    refusal_ok = bool(verdict.get("refused")) == q["should_refuse"]
    return refusal_ok and not any(violated) and (not included or all(included))

# 人工抽查判不合格的示範答案：{題號: {第幾次寫的}}，那幾次的答案丟掉，讓老師再寫
# 第 1 版判掉的是 t0132 第 2 次、t0046 第 4 次（第 2 版全部重寫，那幾次的答案已經不用）；
# t0112 每次都沒說明甲類、乙類是哪些出國，是題目的問題，第 2 版改在 question_review.py 刪掉整題
# 第 2 版：t0212 問盤點（第三十一點，沒搜到），老師拿第五十六點的定期檢核來答，剛好都是每年一次，評審放過
MANUAL_REJECT: dict[str, set[int]] = {"t0212": {1}}

# 老師偶爾冒出一個簡體字（「不適用于」），意思不會變，直接換成繁體；很多字都是簡體的照樣篩掉（SIMPLIFIED）
# 第 2 版（041）：1,368 筆答案裡 17 筆有 SIMPLIFIED 沒收的簡體字，「于」最多
STRAY_SIMPLIFIED = str.maketrans("于并内异标报万", "於並內異標報萬")

# 依據那一行：另起一行，「依據：」開頭
BASIS_LINE = re.compile(r"\n\s*依據[：:]")
# 說「資料沒寫」的句子不用標引用：只有一部分資訊的題目，要說明缺什麼
ABSENT = re.compile(r"找不到|無法(判斷|確定|得知)|並未|沒有.{0,20}(寫|規定|提到|說明|明定|記載)|"
                    r"未.{0,6}(提及|載明|規定|說明|明定|記載|詳述)")
# 開頭一句很短、沒有數字的結論可以不標，例如「不行。」：038 的理想回答就是這樣寫，依據在下一句
SHORT_CONCLUSION = 20


# 依據那一行之前，每一句都要標段落編號（041）：第 1 版的示範答案有一半有句子沒標，模型照著學
def uncited_sentences(answer: str) -> list[str]:
    body = BASIS_LINE.split(answer)[0]
    # 編號標在句號後面（「……子系統。[3][4]」）也算這一句的：先搬到句號前面再切句
    body = re.sub(r"([。！？])\s*((?:\[\d+\])+)", r"\2\1", body)
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])|\n", body) if s.strip()]
    return [s for i, s in enumerate(sentences)
            if not re.search(r"\[\d+\]", s) and not ABSENT.search(s)
            and not (i == 0 and len(s) <= SHORT_CONCLUSION and not re.search(r"\d", s))]


# 評審偶爾把幾項重點合併、或改寫成回答裡的句子（第 2 版第一次寫的 251 題裡 7 題），項目對不上就全部算沒提到：
# 對不上時提醒照抄項目，再問一次（跟 eval/run.py 評審漏了欄位時再問一樣）
ALIGN_REMINDER = "\n\n注意：must_include、must_not 的每一項，item 要照抄上面列的原文，一項對一項，不要合併或改寫。"


def misaligned(verdict: dict, q: dict) -> bool:
    judged = verdict.get("must_include", [])
    items = {v.get("item") for v in judged}
    return len(judged) != len(q["must_include"]) and not all(m in items for m in q["must_include"])


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
    if verdict is not None and misaligned(verdict, q):
        verdict = parse_json(chat(client, [{"role": "user", "content": prompt + ALIGN_REMINDER}], NO_THINK,
                                  timeout=300)) or verdict
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
    # 第 2 版第一次寫的 251 題裡 32 題有：系統會把超出範圍的編號丟掉，使用者點不到出處
    if any(not 1 <= int(n) <= len(item["seen"]) for n in re.findall(r"\[(\d+)\]", answer)):
        reasons.append("標的編號不是段落編號")
    if not q["should_refuse"]:
        if uncited_sentences(answer):
            reasons.append("有句子沒標引用")
        if not BASIS_LINE.search(answer):
            reasons.append("依據沒有另起一行")
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


# 不矛盾的評審：量過（docs/decisions/039），gpt-4o 判成矛盾的 19 個，不思考放過 7 個、思考放過 3 個
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
        answer = answer and answer.translate(STRAY_SIMPLIFIED)
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
    parser.add_argument("--ids", nargs="+", help="只寫這幾題（先小量試新的規定），結果另外存成 *_pilot.jsonl")
    args = parser.parse_args()
    if not openai_client.embedding_client.base_url.host.startswith("localhost"):
        raise SystemExit("要先設好企業內部版的 EMBEDDING_* 環境變數：訓練資料要用本機的轉向量模型搜段落，跟上線時一樣")
    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line]
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]

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
        # 第 2 次起只重寫不合格的；有答案、但沒搜到出題那份規章或寫著答案那一段的不重寫：段落裡沒有答案，再寫也不會對
        todo = [item for item in items if not item.get("passed") and (
            attempt == 0 or item["q"]["should_refuse"]
            or (set(item["q"]["sources"]) & set(item["codes"]) and evidence_retrieved(item)))]
        if not todo:
            break
        print(f"第 {attempt + 1} 次開始：要寫 {len(todo)} 題", flush=True)
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
    suffix = "_pilot" if args.ids else ""
    (DATA / f"answers{suffix}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out),
                                                encoding="utf-8")
    (DATA / f"examples{suffix}.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in examples),
                                                 encoding="utf-8")
    print(f"合格 {len(examples)} / {len(out)}（{len(examples) / len(out) * 100:.0f}%）")
    for reason in sorted({r for row in out for r in row["reasons"]}):
        print(f"  {reason}：{sum(reason in row['reasons'] for row in out)}")


if __name__ == "__main__":
    main()
