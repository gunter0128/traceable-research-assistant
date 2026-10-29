# 出題：讓 Qwen（思考模式）讀臺北市政府的規章，產生訓練用的題目和標準答案（docs/decisions/039）
#
# 標準答案的根據是規章原文，不是 Qwen 自己的知識：每題都要附上依據的原文，程式檢查
#   1. 原文真的出現在規章裡（一字不差；PDF 換行、空白不算）
#   2. 一定要提到的重點裡的數字，真的出現在原文裡
#   3. 太像的題目只留一題
# 沒通過的整題丟掉，原因記在 rejected.jsonl。做法跟勞動法規那套考卷一樣（docs/decisions/031）
#
# 題型跟考卷一樣 7 種，有些要給 Qwen 看兩份規章才出得來：
#   一份：一般題、條件題、身分題
#   兩份：版本題（已廢止 + 現行）、適用範圍題（全市府 + 某個機關）、跨文件題（同主題的兩份）
#   主題清單：沒有答案的題目（規章沒寫的事）——沒辦法用程式證明真的沒寫；如果其實有寫，之後老師會答出來，
#     評審對照「應該說找不到」會判不合格丟掉，不會混進訓練資料
#
# 第 2 批（--more，docs/decisions/041）：第 1 版訓練出來的模型最常錯在「選錯適用的規定」（身分、版本、子機關）
#   只出用到新下載規章的題目，另外每份現行規章再出 2 題身分題（PERSONA），接在原本的題目後面，題號接著編
#
# 用法（專案資料夾，實驗室的 Qwen 和轉接要開著）：
#   .venv\Scripts\python -m train.make_questions --pilot     先試 10 份規章左右，約 20 題，給人看
#   .venv\Scripts\python -m train.make_questions             全部，約 300 題
#   .venv\Scripts\python -m train.make_questions --more      第 2 批

import argparse
import difflib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from app.core.pdf_parser import extract_text
from eval.common import normalize, numbers, quote_found
from eval.run import QWEN_SAMPLING
from train.llm import chat, parse_json

DATA = Path(__file__).parent / "data" / "taipei"
TYPES = {"general", "conditional", "persona", "version", "scope", "cross_doc", "no_answer"}
MAX_CHARS = 7000  # 一次給 Qwen 看的規章長度上限：太長的規章隨機取一段（開頭的名稱、日期一定保留）
# 某個機關自己的規章：名稱裡有機關（局、處、醫院……，「處理」「館舍」這種不算）；有「各機關」的是全市府適用的
BUREAU = re.compile(r"局|處(?!理|分|置|罰|所)|醫院|館(?!舍)|中心|事務所|大隊|學校|國民中學|國民小學|幼稚園")

COMMON_RULES = """規則：
- 只用繁體中文
- 問題要像同仁平常說話：口語、不要照抄條文的用語、不要提條號
- 每一題都要能單獨看懂：說清楚問的是什麼事（例如「學校辦出國訪問的實施計畫」），不要用「那」「這個」「那邊」指代沒講清楚的東西
- 標準答案只能根據規章內容；先給結論，100 字以內；直接講規定的內容，不要提「文件一」「文件二」這種說法
- must_include：回答這一題一定要有的關鍵事實，1～4 個，盡量是具體的數字（連同單位，例如「6 小時」）、對象、可以或不可以；只放必要的，不要把原文的每個細節都放進來
- must_not：別人可能會講錯的「錯誤說法」本身，例如「可以把公務車開回家」「每年盤點兩次」；不是給評審的指示（不要寫「不要提……」）。沒有就給空的 []
- evidence：依據的原文，從規章裡一字不差地複製連續的一句或幾句，中間不能用「……」省略；每題 1～3 段
- 規章的內容不夠出某一題，就不要出那一題，不要硬湊"""

SINGLE = """你要幫「規章問答系統」出訓練用的題目。下面是臺北市政府的一份內部規章。請想像你是臺北市政府或所屬機關的同仁，針對這份規章出 3 題：
1. general：直接問規章裡的一項規定
2. conditional：答案會因情況而不同（例如對象、金額、期間），問題沒說明是哪種情況，標準答案要分別列出
3. persona：問題裡說明了提問者的身分或情況，標準答案只給適用他的那一個

{rules}

輸出 JSON：{{"questions": [{{"type": "general", "question": "", "answer": "", "must_include": [], "must_not": [], "evidence": [""]}}]}}

規章（檔名：{file}）：
{text}"""

# 第 2 批：專門出身分題。一題問到適用的人，一題問到不適用、或適用另一種規定的人：模型要練的是判斷「哪一條適用他」
PERSONA = """你要幫「規章問答系統」出訓練用的題目。下面是臺北市政府的一份內部規章。請想像你是臺北市政府或所屬機關的同仁。
如果這份規章對不同的對象、身分、職級、單位、期間或情況有不同的規定（包括某些人不適用、要比照或準用別的規定），出 2 題 persona：
問題裡說明提問者的身分或情況，標準答案只給適用他的規定；其中一題的提問者要是「規定不適用他」或「適用另一種規定」的人，
標準答案要說明他適用哪一種、或為什麼不適用；must_not 放套用到其他身分的說法。
如果規章沒有因人或情況而不同的規定，回傳空的 questions。

{rules}

輸出 JSON：{{"questions": [{{"type": "persona", "question": "", "answer": "", "must_include": [], "must_not": [], "evidence": [""]}}]}}

規章（檔名：{file}）：
{text}"""

PAIR_TASKS = {
    "version": "文件一是已經停止適用的舊規定，文件二是現行的規定。如果兩份規範的是同一件事，出 1～2 題 version：同仁問的事情新舊規定不同，"
               "標準答案給現行的規定，並說明舊規定已經停止適用；must_not 放舊規定的說法。如果兩份規範的不是同一件事，回傳空的 questions。",
    "scope": "文件一是全市府適用的規定，文件二只適用某一個機關。出 2 題 scope：提問者說明自己在哪一個機關服務，一題是文件二那個機關的同仁，"
             "一題是別的機關的同仁；標準答案只給適用他的規定，並一句話說明另一份為什麼不適用；如果兩份規定的內容不同，must_not 放不適用的那份的說法。"
             "如果兩份規範的事情完全無關，回傳空的 questions。",
    "cross_doc": "出 1 題 cross_doc：要同時用到兩份文件的內容才答得完整的問題。這題要是同仁在同一件事上自然會問到的，"
                 "如果同仁不會在同一個問題裡一起問這兩份文件的內容，回傳空的 questions，不要硬湊。",
}
PAIR = """你要幫「規章問答系統」出訓練用的題目。下面是臺北市政府的兩份內部規章，請想像你是臺北市政府或所屬機關的同仁。
{task}

{rules}
- evidence 的每一段要註明出自哪一份：{{"doc": 1 或 2, "quote": "原文"}}

輸出 JSON：{{"questions": [{{"type": "{qtype}", "question": "", "answer": "", "must_include": [], "must_not": [], "evidence": [{{"doc": 1, "quote": ""}}]}}]}}

文件一（檔名：{file1}）：
{text1}

文件二（檔名：{file2}）：
{text2}"""

NO_ANSWER = """你要幫「規章問答系統」出訓練用的題目。下面是臺北市政府「{topic}」這個主題的規章清單和每份的開頭。
請想 4 個同仁真的可能會問、跟這個主題有關，但這些規章都沒有規定的問題。不要問跟主題完全無關的事，也不要問清單裡規章明顯有寫的事。
標準答案都是「規章中沒有相關規定」。問題要像同仁平常說話。

輸出 JSON：{{"questions": [{{"type": "no_answer", "question": "", "answer": "規章中沒有相關規定"}}]}}

規章清單：
{listing}"""


def load_docs() -> list[dict]:
    cache = DATA / "texts.json"
    docs = [json.loads(line) for line in (DATA / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    texts = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    for d in docs:
        if d["file"] not in texts:
            texts[d["file"]] = extract_text(DATA / "pdfs" / d["file"])
        d["text"] = texts[d["file"]]
        d["code"] = d["file"].split("_")[0]
        d["bureau"] = bool(BUREAU.search(d["name"].replace("臺北市政府", "", 1))) and "各機關" not in d["name"]
    cache.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return docs


# 太長的規章取一段：開頭（名稱、修正日期、沿革）一定保留，其餘從隨機的位置取
def excerpt(text: str, rng: random.Random) -> str:
    if len(text) <= MAX_CHARS:
        return text
    head, body = text[:300], text[300:]
    start = rng.randrange(0, len(body) - (MAX_CHARS - 300))
    start = body.rfind("\n", 0, start) + 1  # 從一行的開頭開始
    return head + "\n……\n" + body[start:start + MAX_CHARS - 300]


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# 兩份規章是不是在講同一類事：比名稱，但先拿掉每份都有的字（臺北市政府、要點、注意事項……），不然都會很像
# 試出題時，名稱不像的兩份硬配成一題（例如「耕地減免地租」配「採購契約變更」），題目很不自然
BOILERPLATE = re.compile(r"臺北市政府|臺北市|及所屬各機關（構）學校|所屬各機關|各機關學校|各機關|暨|及|之|要點|注意事項|辦法|規定|"
                         r"規範|作業|管理|原則|實施|須知|程序|一覽表|自治條例|基準")
PAIR_MIN_SIMILARITY = 0.4  # 量過：0.4 以上的都是真的相關（例如「公務車輛使用」配「警察局公務車輛使用」0.80）
# 某個機關的規章配全市府的，名稱本來就差比較多（「財政局使用菸酒查緝專用公務車輛」配「公務車輛使用」0.38），放寬一點
SCOPE_MIN_SIMILARITY = 0.37


def related(a: dict, b: dict) -> float:
    return similar(BOILERPLATE.sub("", a["name"]), BOILERPLATE.sub("", b["name"]))


# 要問 Qwen 的出題工作：[(題型, 提示, 這次用到的文件)]
def plan(docs: list[dict], pilot: bool) -> list[tuple[str, str, list[dict]]]:
    rng = random.Random(2026)
    current = [d for d in docs if not d["abolished"] and len(d["text"]) >= 500]
    abolished = [d for d in docs if d["abolished"] and len(d["text"]) >= 300]
    tasks = []
    singles = rng.sample(current, 10) if pilot else current  # 正式出題：每一份有內容的現行規章都出
    for d in singles:
        tasks.append(("single", SINGLE.format(rules=COMMON_RULES, file=d["file"], text=excerpt(d["text"], rng)), [d]))

    def pair(qtype, first, second):
        prompt = PAIR.format(task=PAIR_TASKS[qtype], rules=COMMON_RULES, qtype=qtype,
                             file1=first["file"], text1=excerpt(first["text"], rng),
                             file2=second["file"], text2=excerpt(second["text"], rng))
        tasks.append((qtype, prompt, [first, second]))

    # 兩份一組的題型：只配同主題、而且名稱真的相關的（related ≥ PAIR_MIN_SIMILARITY），配不到就不出
    def best(doc, candidates, minimum=PAIR_MIN_SIMILARITY):
        candidates = [c for c in candidates if related(doc, c) >= minimum]
        return max(candidates, key=lambda c: related(doc, c)) if candidates else None

    # 版本：已廢止的規章配取代它的現行規章
    for old in rng.sample(abolished, min(6 if pilot else len(abolished), len(abolished))):
        new = best(old, [d for d in current if d["topic"] == old["topic"]])
        if new:
            pair("version", old, new)
    # 適用範圍：某個機關的規章配全市府的規章
    bureau = [d for d in current if d["bureau"]]
    for own in rng.sample(bureau, min(6 if pilot else len(bureau), len(bureau))):
        citywide = best(own, [d for d in current if d["topic"] == own["topic"] and not d["bureau"]], SCOPE_MIN_SIMILARITY)
        if citywide:
            pair("scope", citywide, own)
    # 跨文件：同主題、名稱相關的兩份現行規章
    topics = sorted({d["topic"] for d in current})
    pairs = [(a, b) for i, a in enumerate(current) for b in current[i + 1:]
             if a["topic"] == b["topic"] and related(a, b) >= PAIR_MIN_SIMILARITY]
    for a, b in rng.sample(pairs, min(3 if pilot else 40, len(pairs))):
        pair("cross_doc", a, b)
    # 沒有答案：每個主題一次，每次 4 題
    for topic in topics[:1] if pilot else topics:
        listing = "\n".join(f"- {d['name']}：{' '.join(d['text'][:120].split())}" for d in docs if d["topic"] == topic)
        tasks.append(("no_answer", NO_ANSWER.format(topic=topic, listing=listing), []))
    return tasks


# 第 2 批：只出用到新規章的題目（至少一份是新的），另外每份現行規章（新舊都算）再出 2 題身分題
# 隨機種子換一個：長的規章會取到跟第 1 批不同的段落
def plan_more(docs: list[dict]) -> list[tuple[str, str, list[dict]]]:
    rng = random.Random(2027)
    current = [d for d in docs if not d["abolished"] and len(d["text"]) >= 500]
    abolished = [d for d in docs if d["abolished"] and len(d["text"]) >= 300]
    new = {d["file"] for d in docs if d.get("batch") == 2}
    tasks = []

    def pair(qtype, first, second):
        prompt = PAIR.format(task=PAIR_TASKS[qtype], rules=COMMON_RULES, qtype=qtype,
                             file1=first["file"], text1=excerpt(first["text"], rng),
                             file2=second["file"], text2=excerpt(second["text"], rng))
        tasks.append((qtype, prompt, [first, second]))

    def best(doc, candidates, minimum=PAIR_MIN_SIMILARITY):
        candidates = [c for c in candidates if related(doc, c) >= minimum]
        return max(candidates, key=lambda c: related(doc, c)) if candidates else None

    for d in current:
        if d["file"] in new:
            tasks.append(("single", SINGLE.format(rules=COMMON_RULES, file=d["file"], text=excerpt(d["text"], rng)), [d]))
    # 版本：新的舊版配現行的；舊的舊版只在配到的現行規章是新的時候才出（配舊的那組第 1 批出過了）
    for old in abolished:
        newest = best(old, [d for d in current if d["topic"] == old["topic"]])
        if newest and (old["file"] in new or newest["file"] in new):
            pair("version", old, newest)
    # 適用範圍：同上，有一份是新的才出
    for own in [d for d in current if d["bureau"]]:
        citywide = best(own, [d for d in current if d["topic"] == own["topic"] and not d["bureau"]], SCOPE_MIN_SIMILARITY)
        if citywide and (own["file"] in new or citywide["file"] in new):
            pair("scope", citywide, own)
    pairs = [(a, b) for i, a in enumerate(current) for b in current[i + 1:]
             if a["topic"] == b["topic"] and related(a, b) >= PAIR_MIN_SIMILARITY and (a["file"] in new or b["file"] in new)]
    for a, b in rng.sample(pairs, min(30, len(pairs))):
        pair("cross_doc", a, b)
    for d in current:
        tasks.append(("persona", PERSONA.format(rules=COMMON_RULES, file=d["file"], text=excerpt(d["text"], rng)), [d]))
    return tasks


# 問的結果存起來、連不上會重試（train/llm.py）：出題要跑 2 小時，中途斷線重跑時做過的直接拿來用
def ask(client: OpenAI, prompt: str) -> list[dict]:
    result = parse_json(chat(client, [{"role": "user", "content": prompt}], QWEN_SAMPLING["think"], timeout=1200))
    return (result or {}).get("questions", [])


# 「不能說」只放錯誤的說法；Qwen 有時會寫成給評審的指示（「不要提條號」），這種拿掉
INSTRUCTION = re.compile(r"^(不要|不可|不得|切勿|勿|別|避免|請勿)")
# 常見的簡體字：Qwen 思考模式偶爾會冒出來。只放繁體中文不會用到的字（「里」「后」「准」這種兩邊都用的不放）
SIMPLIFIED = set("这个们时说为会对发经过还进现实关应规务员资产业让认识议设证请计订许论记际间问题动劳职级别门车费补贴险条办处单历岁龄领导"
                 "与适国签长东华无从体书买卖亲见观视讨质购贸赔轨转轮软输辆边运远连迟选递释铁银错键闭闻队陆难额风飞馆驾验齐拟"
                 "吗么欧检")  # 人工審查時又發現的
# 數字連同單位（「6 小時」「3 年」），normalize 之後空白已經拿掉
UNIT_AMOUNT = re.compile(r"\d+(?:\.\d+)?(?:小時|個月|年|月|日|天|元|人|次|分|%|公里|週|星期)")


# 檢查一題：回傳不合格的原因，合格的話回傳 None
def problem(q: dict, used: list[dict]) -> str | None:
    if q.get("type") not in TYPES or not q.get("question") or not q.get("answer"):
        return "欄位不完整"
    if any(c in SIMPLIFIED for c in q["question"] + q["answer"] + "".join(q.get("must_include") or []) + "".join(q["must_not"])):
        return "有簡體字"
    if q["question"].lstrip().startswith("那"):
        return "題目沒頭沒尾（以「那」開頭）"
    if re.search(r"文件\s*[一二12]", q["question"] + q["answer"]):
        return "寫出出題時的「文件一」「文件二」"
    if q["type"] == "no_answer":
        return None
    if not q.get("must_include"):
        return "沒有重點清單"
    if len(q["must_include"]) > 4:
        return "重點超過 4 個"
    quotes = []
    for e in q.get("evidence") or []:
        doc = used[0] if isinstance(e, str) or len(used) == 1 else used[int(e.get("doc", 1)) - 1]
        quote = e if isinstance(e, str) else e.get("quote", "")
        # 中間用「……」省略的，拆成幾段，每段都要在規章裡找得到
        parts = [p for p in re.split(r"…+|\.{3,}", quote) if len(p.strip()) >= 4]
        if not parts or not all(quote_found(p, [doc["text"]]) for p in parts):
            return f"原文在規章裡找不到：{quote[:40]}"
        quotes.append(quote)
    if not quotes:
        return "沒有附原文"
    # 數字連同單位一起比：試出題時重點寫成「6 年」，原文是「6 小時」，只比數字會放過
    # 千分位的逗號拿掉、中文數字轉成阿拉伯數字（normalize）再比
    evidence = normalize(re.sub(r"(?<=\d),(?=\d{3})", "", "".join(quotes)))
    found = numbers(evidence)
    for item in q["must_include"]:
        text = normalize(re.sub(r"(?<=\d),(?=\d{3})", "", item))
        missing = numbers(text) - found
        missing |= {m for m in UNIT_AMOUNT.findall(text) if m not in evidence}
        if missing:
            return f"重點的數字不在原文裡：{item}（{'、'.join(sorted(missing))}）"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--more", action="store_true", help="第 2 批：接在原本的題目後面")
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    args = parser.parse_args()
    docs = load_docs()
    tasks = plan_more(docs) if args.more else plan(docs, args.pilot)
    print(f"出題工作 {len(tasks)} 個：" + "、".join(f"{t} {sum(x[0] == t for x in tasks)}" for t in dict.fromkeys(x[0] for x in tasks)))
    client = OpenAI(base_url=args.base_url, api_key="not-needed", max_retries=2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda task: ask(client, task[1]), tasks))

    # 第 2 批：題號接著編，太像的題目也要跟第 1 批的比
    before = [json.loads(line) for line in (DATA / "questions.jsonl").read_text(encoding="utf-8").splitlines()
              if line] if args.more else []
    start = max((int(q["id"][1:]) for q in before), default=0)
    kept, rejected, seen = [], [], [normalize(q["question"]) for q in before]
    for (_, _, used), questions in zip(tasks, answers, strict=True):
        for q in questions:
            q["must_not"] = [m for m in q.get("must_not") or [] if not INSTRUCTION.match(m.strip())]
            reason = problem(q, used)
            text = normalize(q.get("question", ""))
            if reason is None and any(similar(text, other) > 0.85 for other in seen):
                reason = "跟別題太像"
            if reason:
                rejected.append({**q, "reason": reason, "docs": [d["file"] for d in used]})
                continue
            seen.append(text)
            kept.append({
                "id": f"t{start + len(kept) + 1:04d}", "type": q["type"], "question": q["question"], "answer": q["answer"],
                "must_include": q.get("must_include", []), "must_not": q.get("must_not", []), "nice_to_have": [],
                "sources": [d["code"] for d in used] if q["type"] != "no_answer" else [],
                "should_refuse": q["type"] == "no_answer", "split": "train",
                "evidence": [e if isinstance(e, str) else e.get("quote", "") for e in q.get("evidence") or []],
            })
    name = "pilot" if args.pilot else "questions"
    mode = "a" if args.more else "w"  # 第 2 批接在原本的檔案後面
    with (DATA / f"{name}.jsonl").open(mode, encoding="utf-8") as out:
        out.write("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in kept))
    with (DATA / f"{name}_rejected.jsonl").open(mode, encoding="utf-8") as out:
        out.write("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in rejected))
    print(f"合格 {len(kept)} 題、不合格 {len(rejected)} 題：{DATA / name}.jsonl")
    for reason in sorted({r['reason'].split('：')[0] for r in rejected}):
        print(f"  {reason}：{sum(r['reason'].startswith(reason) for r in rejected)}")


if __name__ == "__main__":
    main()
