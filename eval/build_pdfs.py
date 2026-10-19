# 把知識庫的文件（簡單的 Markdown）轉成 PDF（B3）
# 用法（專案資料夾）：.venv\Scripts\python -m eval.build_pdfs --corpus enterprise（或 school）
#   讀 eval/<corpus>/docs/，產生到 eval/<corpus>/pdfs/
# 做法：Markdown → HTML（規章的版面）→ 用 Edge 的無頭模式「列印成 PDF」
# 使用者上傳的就是 PDF，用 PDF 測才測得到解析（中文、表格）的問題
# 產生完會用系統自己的解析程式讀回來，檢查規則表（rules.py 的 FACTS）裡的關鍵字都真的在對應的文件裡

import argparse
import html
import importlib
import re
import subprocess
import tempfile
from pathlib import Path

from app.core import pdf_parser
from eval.common import CORPORA

EVAL = Path(__file__).parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: A4; margin: 22mm 20mm; }
body { font-family: 'Microsoft JhengHei', sans-serif; font-size: 11pt; line-height: 1.7; color: #111; }
h1 { font-size: 17pt; text-align: center; margin: 0 0 10px; }
h2 { font-size: 13pt; margin: 18px 0 6px; border-bottom: 1px solid #999; }
.meta { font-size: 9.5pt; color: #333; border: 1px solid #999; padding: 6px 10px; margin-bottom: 14px; }
p { margin: 6px 0; }
table { border-collapse: collapse; margin: 8px 0; }
th, td { border: 1px solid #666; padding: 4px 10px; }
th { background: #eee; }
.footer { margin-top: 28px; font-size: 8.5pt; color: #666; border-top: 1px solid #ccc; padding-top: 6px; }
"""


def inline(text: str) -> str:
    # **粗體** → <b>粗體</b>
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def to_html(markdown: str, footer: str) -> str:
    out, table, items = [], [], []

    def flush():
        # 遇到不是表格 / 清單的行時，把累積的表格或清單輸出
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|[\s\-|:]+\|", r)]  # 去掉 |---| 分隔線
            cells = [[inline(c.strip()) for c in r.strip("|").split("|")] for r in rows]
            head = "".join(f"<th>{c}</th>" for c in cells[0])
            body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells[1:])
            out.append(f"<table><tr>{head}</tr>{body}</table>")
            table.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for line in markdown.splitlines():
        line = line.rstrip()
        if line.startswith("|"):
            table.append(line)
            continue
        if line.startswith("- "):
            items.append(inline(line[2:]))
            continue
        flush()
        if line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("> "):
            out.append(f'<div class="meta">{inline(line[2:])}</div>')
        elif line:
            out.append(f"<p>{inline(line)}</p>")
    flush()
    out.append(f'<div class="footer">{footer}</div>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(out)}</body></html>'


def check_facts(corpus: str, pdfs: Path) -> None:
    # 去掉所有空白再比對：PDF 解析出來的文字可能在任何地方斷行
    rules = importlib.import_module(f"eval.{corpus}.rules")
    facts = getattr(rules, "FACTS", [])
    if not facts:
        print("rules.py 沒有列 FACTS，跳過檢查")
        return
    texts = {pdf.name[:pdf.name.index("_")]: re.sub(r"\s", "", pdf_parser.extract_text(pdf))
             for pdf in pdfs.glob("*.pdf")}
    missing = [(code, fact) for code, fact in facts if re.sub(r"\s", "", fact) not in texts[code]]
    for code, fact in missing:
        print(f"  找不到：{code}「{fact}」")
    print(f"檢查規則表的 {len(facts)} 個關鍵字：{'全部都在' if not missing else f'{len(missing)} 個找不到'}")
    if missing:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, required=True)
    args = parser.parse_args()
    docs, pdfs = EVAL / args.corpus / "docs", EVAL / args.corpus / "pdfs"
    footer = CORPORA[args.corpus]["footer"]

    pdfs.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for md in sorted(docs.glob("*.md")):
            page = Path(tmp) / f"{md.stem}.html"
            page.write_text(to_html(md.read_text(encoding="utf-8"), footer), encoding="utf-8")
            pdf = pdfs / f"{md.stem}.pdf"
            subprocess.run(
                [EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                 f"--print-to-pdf={pdf}", page.as_uri()],
                check=True, capture_output=True,
            )
            print(f"{pdf.name}")
    check_facts(args.corpus, pdfs)


if __name__ == "__main__":
    main()
