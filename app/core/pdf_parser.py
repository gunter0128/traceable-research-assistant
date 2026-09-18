# 把一份 PDF 的文字抓出來。純函式 不碰 DB 不碰 HTTP，跟 security.py 同一種角色

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

class UnreadablePdf(Exception):
    pass


# 何時呼叫：document_service 上傳完 PDF 之後（V1 接回時）
# path: str | Path 代表這個參數可以是字串路徑，也可以是 Path 物件，兩種都收
def extract_text(path: str | Path) -> str:
    try:
        reader = PdfReader(path) # 吃一個檔案路徑 回傳一個「PDF 讀取器」物件(它會自己打開檔案、解析 PDF 內部結構)
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "") # 把每一頁的文字抽取出來或是用空字串代替(避免None)
    # as e：把 pypdf 丟出來的錯誤物件存起來，才能在下面用 str(e) 拿到它的訊息內容
    except PdfReadError as e:
        # 把 pypdf 的錯誤「翻譯」成我們自己的 UnreadablePdf 這樣我們的程式不用在乎他的底層究竟是出甚麼錯
        # str(e) 保留原始錯誤訊息文字；from e 保留原始錯誤在 traceback 裡的完整鏈，方便之後除錯
        raise UnreadablePdf(str(e)) from e
    return "\n".join(pages).strip()
