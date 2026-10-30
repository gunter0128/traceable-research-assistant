# 從全國法規資料庫下載勞動法規，轉成知識庫的原稿（docs/decisions/031）
#
# 用法（專案資料夾）：.venv\Scripts\python -m eval.labor.fetch_laws
#   下載的網頁存在 eval/labor/raw/（不進 git），轉好的原稿寫到 eval/labor/docs/
#   之後跟其他知識庫一樣：python -m eval.build_pdfs --corpus labor
#
# 法規依著作權法第 9 條不是著作權的標的，可以放進專案
# 條文一律從這裡下載，出題時只看下載的條文，不靠記憶：法規常常修正（例如勞工請假規則 115 年 9 月剛修過婚假）

import html
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
RAW = HERE / "raw"
DOCS = HERE / "docs"
SITE = "https://law.moj.gov.tw/LawClass/"

# (代號, 全國法規資料庫的 pcode, 檔名, 舊版的日期)
# 舊版的日期是歷史法規頁面的 lnndate；None = 現行版
LAWS = [
    # 主體：勞工實際會問的
    ("L-01", "N0030001", "勞動基準法", None),
    ("L-02", "N0030002", "勞動基準法施行細則", None),
    ("L-03", "N0030006", "勞工請假規則", None),
    ("L-04", "N0030014", "性別平等工作法", None),
    ("L-05", "N0030015", "性別平等工作法施行細則", None),
    ("L-06", "N0030018", "育嬰留職停薪實施辦法", None),
    ("L-07", "N0030019", "工作場所性騷擾防治措施準則", None),
    ("L-08", "N0030020", "勞工退休金條例", None),
    ("L-09", "N0030028", "最低工資法", None),
    ("L-10", "N0050001", "勞工保險條例", None),
    ("L-11", "N0050002", "勞工保險條例施行細則", None),
    ("L-12", "N0050021", "就業保險法", None),
    ("L-13", "N0050031", "勞工職業災害保險及保護法", None),
    ("L-14", "N0060001", "職業安全衛生法", None),
    ("L-15", "N0060002", "職業安全衛生法施行細則", None),
    ("L-16", "N0090001", "就業服務法", None),
    ("L-17", "N0020007", "勞資爭議處理法", None),
    ("L-18", "N0020012", "大量解僱勞工保護法", None),
    # 陷阱：長得像，但不適用一般勞工（公務人員、教師各有自己的請假規則）
    ("L-19", "S0110016", "公務人員請假規則", None),
    ("L-20", "H0150030", "教師請假規則", None),
    # 舊版：版本題用。勞工請假規則 114 年 12 月修了事假和全勤獎金（115/1/1 施行），115 年 9 月修了婚假（115/10/1 施行）
    ("L-21", "N0030006", "勞工請假規則_112年版", "20230501"),
    ("L-22", "N0030006", "勞工請假規則_114年版", "20251209"),
]


def download(pcode: str, old_version: str | None) -> str:
    RAW.mkdir(exist_ok=True)
    path = RAW / (f"{pcode}_{old_version}.html" if old_version else f"{pcode}.html")
    if not path.exists():
        if old_version:
            url = SITE + "LawOldVer.aspx?" + urllib.parse.urlencode({"pcode": pcode, "lnndate": old_version, "lser": "001"})
        else:
            url = SITE + "LawAll.aspx?" + urllib.parse.urlencode({"pcode": pcode})
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            path.write_bytes(response.read())
        time.sleep(1)  # 不要連續打太快
    return path.read_text(encoding="utf-8")


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# 解析「所有條文」頁面：法規名稱、日期、依原本的順序排好的章節標題和條文
def parse(page: str) -> dict:
    name = re.search(r'id="(?:hlLawName|hlkName)"[^>]*>([^<]+)</a>', page)
    date = re.search(r"<th>(修正日期|公布日期|發布日期)：</th>\s*<td>([^<]+)</td>", page)
    parts = []
    # 章節標題（<div class="h3 char-2">第 一 章 總則</div>）和條文（<div class="row">…）照出現的順序
    # 現行版的條號是連結（<a name=...>），舊版是純文字
    pattern = re.compile(
        r'<div class="h3 char-\d">\s*(?P<chapter>[^<]+?)\s*</div>'
        r'|<div class="col-no">\s*(?:<a[^>]*>)?(?P<no>[^<]+?)(?:</a>)?</div>'
        r'<div class="col-data"><div class="law-article">(?P<body>.*?)</div>\s*</div></div>',
        re.DOTALL,
    )
    for m in pattern.finditer(page):
        if m.group("chapter"):
            parts.append(("chapter", " ".join(m.group("chapter").split())))
        else:
            lines = [_text(line) for line in re.findall(r'<div class="line-\d+[^"]*">(.*?)</div>', m.group("body"), re.DOTALL)]
            parts.append(("article", m.group("no").strip(), [line for line in lines if line]))
    return {
        "name": name.group(1).strip() if name else None,
        "date_label": date.group(1) if date else None,
        "date": " ".join(date.group(2).split()) if date else None,
        "parts": parts,
    }


def to_markdown(law: dict) -> str:
    lines = [f"# {law['name']}", "", f"> {law['date_label']}：{law['date']}｜資料來源：全國法規資料庫", ""]
    for part in law["parts"]:
        if part[0] == "chapter":
            lines += [f"## {part[1]}", ""]
        else:
            _, no, body = part
            lines += [f"**{no}**", *body, ""]
    return "\n".join(lines)


def main():
    DOCS.mkdir(exist_ok=True)
    total = 0
    for code, pcode, filename, old_version in LAWS:
        law = parse(download(pcode, old_version))
        articles = [p for p in law["parts"] if p[0] == "article"]
        if not law["name"] or not law["date"] or not articles:
            raise SystemExit(f"{code} {filename} 解析失敗：{law['name']} {law['date']} {len(articles)} 條")
        markdown = to_markdown(law)
        (DOCS / f"{code}_{filename}.md").write_text(markdown, encoding="utf-8")
        total += len(markdown)
        print(f"{code} {law['name']}｜{law['date_label']} {law['date']}｜{len(articles)} 條、{len(markdown):,} 字")
    print(f"共 {len(LAWS)} 份、{total:,} 字")


if __name__ == "__main__":
    main()
