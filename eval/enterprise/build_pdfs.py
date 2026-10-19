# 把 docs/ 裡的 18 份文件（簡單的 Markdown）轉成 PDF，放到 pdfs/（B3）
# 用法（專案資料夾）：.venv\Scripts\python -m eval.enterprise.build_pdfs
# 做法：Markdown → HTML（公司規章的版面）→ 用 Edge 的無頭模式「列印成 PDF」
# 使用者上傳的就是 PDF，用 PDF 測才測得到解析（中文、表格）的問題

import html
import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
DOCS = HERE / "docs"
PDFS = HERE / "pdfs"
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

FOOTER = "本文件為虛構公司「青嶼科技」之測試資料，僅供系統評估使用，非真實公司規章。"


def inline(text: str) -> str:
    # **粗體** → <b>粗體</b>
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def to_html(markdown: str) -> str:
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
    out.append(f'<div class="footer">{FOOTER}</div>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(out)}</body></html>'


def main():
    PDFS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for md in sorted(DOCS.glob("*.md")):
            page = Path(tmp) / f"{md.stem}.html"
            page.write_text(to_html(md.read_text(encoding="utf-8")), encoding="utf-8")
            pdf = PDFS / f"{md.stem}.pdf"
            subprocess.run(
                [EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                 f"--print-to-pdf={pdf}", page.as_uri()],
                check=True, capture_output=True,
            )
            print(f"{pdf.name}")


if __name__ == "__main__":
    main()
