# 把一份 PDF 的文字抓出來。純函式 不碰 DB 不碰 HTTP，跟 security.py 同一種角色

from pathlib import Path
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

class UnreadablePdf(Exception):
    pass


# 何時呼叫：document_service 把 S3 抓回來的 PDF 內容丟進來解析；測試裡也會直接傳本機路徑
# source: str | Path | BinaryIO 代表可以是本機路徑，也可以是 io.BytesIO 這種「檔案物件」，
# PdfReader 兩種都吃，不用自己先判斷是哪一種再分開寫
def extract_text(source: str | Path | BinaryIO) -> str:
    try:
        reader = PdfReader(source) # 吃一個檔案路徑或檔案物件 回傳一個「PDF 讀取器」物件(它會自己打開、解析 PDF 內部結構)
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "") # 把每一頁的文字抽取出來或是用空字串代替(避免None)
    # as e：把 pypdf 丟出來的錯誤物件存起來，才能在下面用 str(e) 拿到它的訊息內容
    except PdfReadError as e:
        # 把 pypdf 的錯誤「翻譯」成我們自己的 UnreadablePdf 這樣我們的程式不用在乎他的底層究竟是出甚麼錯
        # str(e) 保留原始錯誤訊息文字；from e 保留原始錯誤在 traceback 裡的完整鏈，方便之後除錯
        raise UnreadablePdf(str(e)) from e
    return "\n".join(pages).strip()
