# 擴大評估用的知識庫（docs/decisions/026）：照 eval/<corpus>/catalog.py 的清單產生新文件，再做靜態檢查
#
# 用法（專案資料夾）：
#   .venv\Scripts\python -m eval.expand_docs --corpus enterprise derive     改寫：舊版、子公司（逐字替換，不用 AI）
#   .venv\Scripts\python -m eval.expand_docs --corpus enterprise generate   起草：照大綱交給 gpt-4o（已存在的檔案跳過）
#   .venv\Scripts\python -m eval.expand_docs --corpus enterprise check      靜態檢查：數字、禁止主題、部門、日期、格式
# 內容會不會改變題目的答案，另外用 eval/review_docs.py 檢查

import argparse
import importlib
import re
import sys
from pathlib import Path

from openai import OpenAI

from app.core.config import settings

EVAL = Path(__file__).parent

STYLES = {
    "條文": "條文式：每條用一行「**第 1 條　標題**」，下一行寫內容",
    "手冊": "分章節：先用「## 第一章　…」分章，章底下再用「**第 1 條　標題**」寫條文",
    "說明": "說明文件：用「## 」小標題分成幾段，段落或「- 」清單",
    "問答": "問答集：每組一行「**問：……**」，下一行「答：……」",
    "會議紀錄": "會議紀錄：先列時間、地點、出席、主席、紀錄，再用「## 討論事項」逐案寫「案由」「說明」「決議」",
    "公告": "公告：先寫「**主旨**」一段，再寫「**說明**」並用「- 一、……」「- 二、……」的清單",
    "表格": "以表格為主，表格前面用一兩句話說明",
    "要點": "學校要點：每點一段「一、標題：內容」，細項另起一段「（一）……」",
    "須知": "須知：每點一段「一、標題」，細項另起一段「（一）……」",
    "計畫": "計畫：依序「一、依據」「二、目的」「三、時間」「四、流程」……每點一段，細項另起一段「（一）……」",
}
LENGTHS = {"短": "約 300～500 個中文字", "中": "約 500～900 個中文字", "長": "約 900～1400 個中文字"}

# 靜態檢查：這些詞出現就要人工確認（大多數會是誤報，但漏掉一個就可能改變題目的答案）
WATCH = {
    "enterprise": [r"年終", r"宿舍", r"尾牙", r"認股", r"股票", r"調薪", r"薪資調整", r"停車位", r"VPN",
                   r"特別休假|特休", r"病假|事假|婚假|喪假", r"地區彈性假", r"加班", r"遠距", r"住宿費", r"膳雜費",
                   r"日支費", r"商務艙", r"計程車", r"健康檢查", r"生日禮金", r"結婚禮金", r"喪葬", r"旅遊補助",
                   r"教育訓練補助", r"外接螢幕", r"密碼", r"報銷", r"撥付|撥款", r"彈性上班|到班時間", r"分機", r"試用期"],
    "school": [r"婚假|喪假|事假|病假", r"代課|鐘點", r"午餐費|每餐|元/餐|元／餐", r"同意書", r"帶隊", r"課後照顧",
               r"導護", r"成績|評量|補考", r"醫療院所|醫生證明|診斷證明", r"授課節數", r"分機", r"校外教學"],
}
# 每份文件刻意允許的詞（大綱本來就要求寫這些）
ALLOW = {
    "HR-22": [r"遠距", r"加班"], "HR-23": [r"健康檢查", r"結婚禮金"], "HR-20": [r"彈性上班|到班時間"],
    "HR-17": [r"病假|事假|婚假|喪假"],
    "S-12": [r"代課|鐘點"], "S-14": [r"導護"], "S-21": [r"導護"], "S-26": [r"課後照顧"],
    "S-39": [r"成績|評量|補考", r"校外教學"], "S-40": [r"成績|評量|補考", r"校外教學"],
}
GENERIC_UNITS = ("各部", "該部", "本部", "內部", "外部", "全部", "總部", "辦公室", "實驗室", "會議室", "哺集乳室", "警衛室", "教室")
DEPARTMENTS = {"研發部", "業務部", "人力資源部", "財務部", "資訊部", "行政部", "管理部", "青嶼雲端服務管理部", "台中分公司管理部"}


def load_catalog(corpus):
    return importlib.import_module(f"eval.{corpus}.catalog")


def docs_dir(corpus):
    return EVAL / corpus / "docs"


def find_doc(corpus, code):
    matches = list(docs_dir(corpus).glob(f"{code}_*.md"))
    return matches[0] if len(matches) == 1 else None


def squash(text):
    return re.sub(r"\s", "", text)


# ---------- 改寫 ----------
def derive(corpus):
    catalog = load_catalog(corpus)
    for spec in catalog.DERIVED:
        source = find_doc(corpus, spec["source"])
        text = source.read_text(encoding="utf-8")
        for old, new in spec["replace"]:
            count = text.count(old)
            if count != 1:
                sys.exit(f"{spec['code']}：要替換的原文出現 {count} 次（應該剛好 1 次）：{old[:40]}")
            text = text.replace(old, new)
        if spec.get("rename"):
            # 子公司的文件改了名稱（規定、要點），內文的「本辦法」也要跟著改
            text = text.replace("本辦法", spec["rename"])
        if spec.get("cut_from"):
            index = text.find(spec["cut_from"])
            if index < 0:
                sys.exit(f"{spec['code']}：找不到要刪掉的段落開頭「{spec['cut_from']}」")
            text = text[:index].rstrip() + "\n"
        for fact in spec["facts"]:
            if squash(fact) not in squash(text):
                sys.exit(f"{spec['code']}：改寫後找不到「{fact}」")
        target = docs_dir(corpus) / f"{spec['filename']}.md"
        target.write_text(text, encoding="utf-8")
        print(f"改寫 {target.name}")


# ---------- 起草 ----------
def prompt_for(corpus, catalog, spec, example):
    if corpus == "enterprise":
        first = f"# 青嶼科技股份有限公司　{spec['title']}"
        second = spec["header"] or f"> 文件編號：{spec['code']}｜發布單位：{spec['unit']}｜{spec['date']}｜適用對象：{spec['scope']}"
    else:
        first = f"# {spec['title']}" if spec["header"] else f"# 嵐港市立澄溪國民小學{spec['title']}"
        second = spec["header"] or spec["date"]
    points = "\n".join(f"- {p}" for p in spec["points"])
    facts = "、".join(f"「{f}」" for f in spec["facts"])
    return f"""{catalog.CONTEXT}

{catalog.FORBIDDEN}

請撰寫下面這份文件，只輸出文件本身（Markdown），不要任何說明或前言。

文件標題：{spec['title']}
寫法：{STYLES[spec['style']]}
長度：{LENGTHS[spec['length']]}
內容大綱（每一點都要寫進去，數字照抄，不要改寫成別的數字）：
{points}
以下字串必須原封不動出現在文件中：{facts}

格式規定：
- 第一行固定是：{first}
- 第二行空白，第三行固定是：{second}
- 只能用這些格式：「# 」標題（只有第一行）、「## 」小標題、「**粗體**」、「- 」清單、表格（| 欄 | 欄 |，第二列是 |---|---|）、一般段落
- 不要用「### 」、不要用「1.」編號清單、不要用程式碼區塊、不要加頁尾、不要寫「本文件為虛構」之類的聲明
- 用正體中文、台灣的用語，語氣跟真實的組織內部文件一樣正式

參考格式（同一個組織的另一份文件）：
{example}"""


def generate(corpus, only=None, force=False):
    catalog = load_catalog(corpus)
    example_code = "GEN-04" if corpus == "enterprise" else "S-05"
    example = find_doc(corpus, example_code).read_text(encoding="utf-8")
    client = OpenAI(api_key=settings.openai_api_key)
    for spec in catalog.GENERATED:
        if only and spec["code"] not in only:
            continue
        existing = find_doc(corpus, spec["code"])
        if existing and not force:
            continue
        response = client.chat.completions.create(
            model="gpt-4o", temperature=0.6,
            messages=[{"role": "user", "content": prompt_for(corpus, catalog, spec, example)}],
        )
        text = response.choices[0].message.content.strip()
        text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text).strip() + "\n"
        if existing:
            existing.unlink()
        name = re.sub(r"[\\/:*?\"<>|\s]", "", spec["title"])
        target = docs_dir(corpus) / f"{spec['code']}_{name}.md"
        target.write_text(text, encoding="utf-8")
        print(f"起草 {target.name}（{len(text)} 字）")


# ---------- 靜態檢查 ----------
def check(corpus):
    catalog = load_catalog(corpus)
    problems, notes = [], []
    specs = [(s, False) for s in catalog.GENERATED] + [(s, True) for s in catalog.DERIVED]
    for spec, derived in specs:
        path = find_doc(corpus, spec["code"])
        if path is None:
            problems.append(f"{spec['code']}：找不到檔案")
            continue
        text = path.read_text(encoding="utf-8")
        squashed = squash(text)
        for fact in spec["facts"]:
            if squash(fact) not in squashed:
                problems.append(f"{spec['code']}：缺少「{fact}」")
        lines = text.splitlines()
        if not lines[0].startswith("# "):
            problems.append(f"{spec['code']}：第一行不是標題")
        for line in lines:
            if line.startswith(("###", "```")) or re.match(r"^\d+\.\s", line):
                problems.append(f"{spec['code']}：不支援的格式「{line[:30]}」")
        if "虛構" in text:
            problems.append(f"{spec['code']}：出現「虛構」")
        if derived:
            continue  # 改寫的文件刻意保留原文的主題，只檢查上面幾項
        allowed = ALLOW.get(spec["code"], [])
        for pattern in WATCH[corpus]:
            if pattern in allowed:
                continue
            for match in re.finditer(pattern, text):
                start = max(0, match.start() - 25)
                notes.append(f"{spec['code']}：「{match.group()}」…{text[start:match.end() + 25].replace(chr(10), ' ')}…")
        if corpus == "enterprise":
            # 找出「○○部」「○○室」，排除「部門」「部分」這類不是單位名稱的詞；結尾是 6 個部門之一或常見的泛稱就沒問題
            for unit in set(re.findall(r"[一-鿿]{1,6}[部室](?![門分份署落隊長位])", text)):
                if not unit.endswith(tuple(DEPARTMENTS) + GENERIC_UNITS):
                    notes.append(f"{spec['code']}：單位名稱「{unit}」")
            for year in re.findall(r"(20\d\d) 年", text):
                if int(year) > 2026:
                    problems.append(f"{spec['code']}：日期 {year} 年晚於現在")
        else:
            for year in re.findall(r"(1\d\d) 年", text):
                if int(year) > 116:
                    problems.append(f"{spec['code']}：日期民國 {year} 年晚於現在")
                elif int(year) == 116:
                    notes.append(f"{spec['code']}：出現民國 116 年（本學期的行事曆可以，其他要確認）")
    print(f"=== {corpus}：{len(specs)} 份新文件 ===")
    print(f"問題 {len(problems)} 個")
    for p in problems:
        print("  ✗", p)
    print(f"要人工確認 {len(notes)} 處")
    for n in notes:
        print("  ?", n)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, choices=["enterprise", "school"])
    parser.add_argument("action", choices=["derive", "generate", "check"])
    parser.add_argument("--only", nargs="*", help="只處理這幾份（文件編號）")
    parser.add_argument("--force", action="store_true", help="已存在的檔案也重新起草")
    args = parser.parse_args()
    if args.action == "derive":
        derive(args.corpus)
    elif args.action == "generate":
        generate(args.corpus, args.only, args.force)
    else:
        check(args.corpus)


if __name__ == "__main__":
    main()
