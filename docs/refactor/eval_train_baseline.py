# 重構的比對工具（docs/refactor/A0_BASELINE.md）：eval / train 不需要資料庫、不需要模型的純函式，現在的輸出是什麼
# 改 eval/、train/ 之前跑一次、改完再跑一次，印出來的每一行都要一樣
#
# 用法（專案資料夾，Git Bash）：
#   .venv/Scripts/python.exe -I docs/refactor/eval_train_baseline.py . <輸出資料夾>
#   輸出資料夾放在專案外面（例如暫存資料夾）：這支程式只讀專案的檔案，產生的東西都寫到那裡，不改專案、不改資料檔
# 需要本機的 eval/data/results/、train/data/taipei/（不進 git）；會 import app，所以 .env 要在
#
#   1. 評估題目：重新產生 questions.jsonl / questions.md，跟 git 裡的逐字元比對
#   2. build_pdfs.to_html：每份知識庫原稿轉成的 HTML，算出指紋
#   3. citation_checks：用 eval/data/results 存的逐題結果重算，跟當時存下來的比對
#   4. train 出題規劃 plan() / plan_more()：算出指紋（固定隨機種子，每次跑都應該一樣）
#   5. question_review：重新產生審查後的題目，跟本機的 questions_reviewed.jsonl 比對
import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(root))


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ---------- 1. 評估題目 ----------
from eval.common import (
    CORPORA,
    assign_splits,
    citation_checks,
    write_question_files,
)

for corpus in ("enterprise", "labor"):
    module = importlib.import_module(f"eval.{corpus}.questions")
    folder = root / "eval" / corpus
    target = out / "questions" / corpus
    target.mkdir(parents=True, exist_ok=True)
    assign_splits(module.QUESTIONS, previous=folder / "questions.jsonl")
    write_question_files(module.QUESTIONS, target, "x", "x")
    same_jsonl = (target / "questions.jsonl").read_bytes() == (folder / "questions.jsonl").read_bytes()
    # questions.md 的標題、指令是 main() 傳的，這裡傳 "x"，所以只比第 3 行之後
    md_new = (target / "questions.md").read_text(encoding="utf-8").splitlines()[3:]
    md_old = (folder / "questions.md").read_text(encoding="utf-8").splitlines()[3:]
    print(f"[1] {corpus} 題目：jsonl 一致={same_jsonl}，md 內容一致={md_new == md_old}")

# ---------- 2. to_html ----------
from eval.build_pdfs import to_html

html_prints = {}
for corpus, config in CORPORA.items():
    for md in sorted((root / "eval" / corpus / "docs").glob("*.md")):
        html_prints[md.name] = fingerprint(to_html(md.read_text(encoding="utf-8"), config["footer"]))
print(f"[2] to_html：{len(html_prints)} 份，整體指紋 {fingerprint(html_prints)}")

# ---------- 3. citation_checks ----------
TODAY = {corpus: importlib.import_module(f"eval.{corpus}.rules").TODAY for corpus in CORPORA}
rows_checked = rows_matched = rows_without_saved = 0
all_checks = []
for path in sorted((root / "eval" / "data" / "results").glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    corpus = data["meta"].get("corpus")
    if corpus not in TODAY:
        continue
    today = TODAY[corpus]
    # 108 份是 {"runs": [[逐題]...]}；另外 16 份是單輪的 {"rows": [逐題]}（只量檢索或只做檢查的實驗）
    runs = data["runs"] if "runs" in data else [data["rows"]]
    for run in runs:
        for row in run:
            # 只有完整評估的逐題結果才有答案、題目、模型看到的段落；檢索實驗的沒有
            if not all(key in row for key in ("passages", "question", "response")):
                continue
            given = f"{row['question']}\n今天日期：{today.year} 年 {today.month} 月 {today.day} 日"
            checks = citation_checks(row["response"], row["passages"], given)
            all_checks.append(checks)
            rows_checked += 1
            if "citation_checks" not in row:
                rows_without_saved += 1
            elif row["citation_checks"] == checks:
                rows_matched += 1
print(f"[3] citation_checks：重算 {rows_checked} 題，跟當時存的一樣 {rows_matched} 題，"
      f"當時沒存的 {rows_without_saved} 題，整體指紋 {fingerprint(all_checks)}")

# ---------- 4. train 出題規劃 ----------
# load_docs() 會把 texts.json 寫回 DATA，所以把 DATA 換成輸出資料夾裡的副本，不動本機的資料檔
from train import make_questions

data_copy = out / "taipei"
data_copy.mkdir(exist_ok=True)
for name in ("manifest.jsonl", "texts.json"):
    shutil.copy(root / "train" / "data" / "taipei" / name, data_copy / name)
make_questions.DATA = data_copy
docs = make_questions.load_docs()


def summarize(tasks):
    return [(qtype, prompt, [d["file"] for d in used]) for qtype, prompt, used in tasks]


plan_full = summarize(make_questions.plan(docs, pilot=False))
plan_pilot = summarize(make_questions.plan(docs, pilot=True))
plan_more = summarize(make_questions.plan_more(docs))
print(f"[4] plan：{len(plan_full)} 個工作 {fingerprint(plan_full)}；pilot {len(plan_pilot)} 個 {fingerprint(plan_pilot)}；"
      f"plan_more {len(plan_more)} 個 {fingerprint(plan_more)}")

# ---------- 5. question_review ----------
from train import question_review

review_copy = out / "review"
review_copy.mkdir(exist_ok=True)
shutil.copy(root / "train" / "data" / "taipei" / "questions.jsonl", review_copy / "questions.jsonl")
question_review.DATA = review_copy
question_review.main()
same_review = (review_copy / "questions_reviewed.jsonl").read_bytes() == (
    root / "train" / "data" / "taipei" / "questions_reviewed.jsonl"
).read_bytes()
print(f"[5] question_review：重新產生的結果跟本機的 questions_reviewed.jsonl 一致={same_review}")
