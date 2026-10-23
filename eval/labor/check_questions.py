# 檢查勞動法規題目的標準答案有沒有出錯（docs/decisions/031）
#
# 用法（專案資料夾）：
#   .venv\Scripts\python -m eval.labor.check_questions            程式檢查（不花錢）
#   .venv\Scripts\python -m eval.labor.check_questions --sweep    另外列出「提到同樣關鍵字的其他條文」，人工確認有沒有但書或例外
#   .venv\Scripts\python -m eval.labor.check_questions --blind    另外請 gpt-4o 只看條文盲答，答得跟標準答案不一樣的列出來
#
# 程式檢查的項目：
#   1. 依據的原文跟下載的法規一字不差（條號也要對）
#   2. 題目的「應該引用」都有附上原文
#   3. 「一定要提到」的關鍵數字真的出現在原文裡（中文數字先轉成阿拉伯數字再比）
#   4. 需要計算的答案，用程式重算一次

import argparse
import re
from dataclasses import asdict
from pathlib import Path

from eval.common import normalize, numbers
from eval.labor.questions import COMPUTED, EVIDENCE, QUESTIONS

DOCS = Path(__file__).parent / "docs"

# 有計算的項目：數字是算出來的，不會出現在原文裡，改由 COMPUTED 檢查
COMPUTED_ITEMS = {("p11", "2.625 個月"), ("x01", "23 個月"), ("x08", "50 人")}

# --sweep：每題要找哪些關鍵字
SWEEP = {
    "c01": ["特別休假"], "p01": ["特別休假"], "p02": ["特別休假"], "p03": ["特別休假"], "x05": ["特別休假"],
    "c02": ["延長工作時間"], "p04": ["延長工作時間"], "c03": ["休息日"], "c08": ["延長工作時間"],
    "c04": ["喪假"], "s01": ["喪假"], "s10": ["喪假"],
    "c05": ["預告"], "p07": ["預告"], "c13": ["預告"],
    "c06": ["流產"], "p05": ["流產"], "s06": ["流產"], "c07": ["產假"], "p06": ["產假"], "s05": ["產假"],
    "c09": ["失業給付"], "p08": ["失業給付"], "x04": ["失業給付", "非自願離職"],
    "c10": ["育嬰留職停薪"], "v09": ["育嬰留職停薪"], "x06": ["育嬰留職停薪"], "g10": ["育嬰留職停薪"],
    "c11": ["自請退休"], "p09": ["自請退休"], "p10": ["自請退休"],
    "c12": ["傷病假", "病假"], "s09": ["病假"], "s08": ["病假"], "x02": ["生理假"], "x10": ["傷病"],
    "v01": ["婚假"], "v02": ["婚假"], "v07": ["婚假"], "s02": ["婚假"], "x09": ["婚假"],
    "v03": ["事假"], "v08": ["事假"], "s03": ["事假"], "s04": ["事假"], "x03": ["家庭照顧假"],
    "v04": ["全勤獎金"], "v05": ["全勤獎金"], "v06": ["不利之處分"],
    "g01": ["正常工作時間"], "g02": ["休息"], "g03": ["十二小時"], "g04": ["出勤紀錄"], "g05": ["競業禁止"],
    "g06": ["預扣"], "g07": ["妊娠"], "g08": ["百分之六"], "g09": ["服務證明書"],
    "p11": ["資遣費"], "x01": ["資遣費"], "p12": ["減少工作時間"], "p13": ["減少工作時間"],
    "s07": ["適用對象"], "s11": ["童工"], "s12": ["身心調適假"], "s13": ["休假"],
    "x07": ["責任制"], "x08": ["大量解僱"],
}


# ---------- 讀法規：{代號: {條號: 內文}} ----------
def load_laws() -> dict[str, dict[str, str]]:
    laws = {}
    for md in sorted(DOCS.glob("*.md")):
        code = md.name.split("_")[0]
        articles = {}
        for block in re.split(r"\n(?=\*\*第 )", md.read_text(encoding="utf-8")):
            m = re.match(r"\*\*(第 [\d\-]+ 條)\*\*\n(.*)", block, re.DOTALL)
            if m:
                # 去掉後面接著的章節標題（## 第 二 章 ……）
                articles[m.group(1)] = re.split(r"\n## ", m.group(2))[0].strip()
        laws[code] = articles
    return laws


def check(laws) -> list[str]:
    problems, manual = [], []
    for q in QUESTIONS:
        evidence = EVIDENCE.get(q.id, [])
        if q.should_refuse:
            if evidence:
                problems.append(f"{q.id}：沒有答案的題目不該有依據")
            continue
        if not evidence:
            problems.append(f"{q.id}：沒有附依據的原文")
            continue
        # 1. 原文一字不差
        for code, article, quote in evidence:
            text = laws.get(code, {}).get(article)
            if text is None:
                problems.append(f"{q.id}：找不到 {code} {article}")
            elif quote not in text:
                problems.append(f"{q.id}：{code} {article} 裡沒有這段原文：{quote[:30]}…")
        # 2. 應該引用的文件都有附原文
        cited_codes = {code for code, _, _ in evidence}
        for source in q.sources:
            code, article = source.split(" ", 1)
            if code not in cited_codes:
                problems.append(f"{q.id}：應該引用 {source}，但沒有附這份文件的原文")
            if article not in laws.get(code, {}):
                problems.append(f"{q.id}：應該引用的 {source} 不存在")
        # 3. 關鍵數字在原文裡
        quotes = normalize("".join(quote for _, _, quote in evidence))
        for item in q.must_include + q.nice_to_have:
            if (q.id, item) in COMPUTED_ITEMS:
                continue
            target = normalize(item)
            if target in quotes:
                continue
            item_numbers = numbers(target)
            if item_numbers and item_numbers <= numbers(quotes):
                continue
            if item_numbers:
                problems.append(f"{q.id}：「{item}」的數字不在原文裡")
            else:
                manual.append(f"{q.id}「{item}」")
    # 4. 計算
    for key, (what, computed, written) in COMPUTED.items():
        if abs(float(computed) - float(written)) > 1e-9:
            problems.append(f"{key}：{what} 算出來是 {computed}，答案寫 {written}")
    print(f"沒有數字、要靠意思判斷的項目（盲答時會一起檢查）：{len(manual)} 個")
    return problems


def sweep(laws) -> None:
    for q in QUESTIONS:
        cited = {(code, article) for code, article, _ in EVIDENCE.get(q.id, [])}
        hits = []
        for keyword in SWEEP.get(q.id, []):
            for code, articles in laws.items():
                for article, text in articles.items():
                    if keyword in text and (code, article) not in cited:
                        hits.append(f"{code} {article}")
        if hits:
            print(f"{q.id} {q.question}\n    其他提到「{'、'.join(SWEEP[q.id])}」的條文：{'、'.join(sorted(set(hits)))}")


# ---------- 盲答：只給條文全文，不給標準答案 ----------
BLIND_PROMPT = """今天是 2026 年 10 月 24 日（民國 115 年 10 月 24 日）。以下是台灣的法規條文，只能根據這些條文回答問題，
答案會因情況而不同時，列出各種情況。問題問的人沒有說自己是公務人員或教師的，就當作一般勞工。

條文：
{articles}

問題：{question}"""


def blind(laws) -> None:
    # 這裡才 import：只有 --blind 要用到 OpenAI 和評審。eval.run 要比 app 先 import（它會把資料庫換成臨時的）
    # isort: off
    from eval.run import judge
    # isort: on
    from openai import OpenAI

    from app.core.config import settings
    from eval.common import CORPORA

    client = OpenAI(api_key=settings.openai_api_key)
    disagreements = 0
    for q in QUESTIONS:
        if q.should_refuse:
            continue
        refs = sorted({(code, article) for code, article, _ in EVIDENCE[q.id]})
        articles = "\n\n".join(f"【{code} {article}】\n{laws[code][article]}" for code, article in refs)
        answer = client.chat.completions.create(
            model="gpt-4o", temperature=0,
            messages=[{"role": "user", "content": BLIND_PROMPT.format(articles=articles, question=q.question)}],
        ).choices[0].message.content
        verdict = judge(client, CORPORA["labor"]["domain"], asdict(q), answer, [])
        missing = [v["item"] for v in verdict["must_include"] if not v.get("present")]
        wrong = [v["item"] for v in verdict["must_not"] if v.get("present")]
        if missing or wrong:
            disagreements += 1
            print(f"\n✗ {q.id} {q.question}\n  標準答案：{q.answer}\n  盲答：{answer}\n  缺：{missing}  說錯：{wrong}")
    print(f"\n盲答跟標準答案不一致：{disagreements} 題（逐題看是誰錯）")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--blind", action="store_true")
    args = parser.parse_args()
    laws = load_laws()
    problems = check(laws)
    for p in problems:
        print("  ✗", p)
    answerable = sum(1 for q in QUESTIONS if not q.should_refuse)
    print(f"程式檢查：{len(QUESTIONS)} 題（有答案的 {answerable} 題），{'全部通過' if not problems else f'{len(problems)} 個問題'}")
    if args.sweep:
        sweep(laws)
    if args.blind:
        blind(laws)
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
