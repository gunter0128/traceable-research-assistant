# 把一份 PDF 的文字抓出來。純函式 不碰 DB 不碰 HTTP，跟 security.py 同一種角色

import re
import unicodedata
from pathlib import Path
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# 康熙部首（U+2F00–U+2FDF）和中日韓部首補充（U+2E80–U+2EFF）
# 某些字型產生的中文 PDF（例如 Chrome、部分 Word 匯出），會把常用字解析成這兩個區段的「部首字元」：
# 「日」變成「⽇」、「用」變成「⽤」，看起來一樣，但對電腦是不同的字，搜尋和轉向量都會出錯
_RADICALS = re.compile(r"[\u2e80-\u2eff\u2f00-\u2fdf]")


# 康熙部首有 Unicode 定義好的對應（NFKC 就轉得回來）；「部首補充」這個區段沒有，只能自己列
# 這幾個是在 PDF 裡實際看過、而且有完整意思的字（長、西、民、青、食、骨）
_SUPPLEMENT = {
    "\u2ed1": "長", "\u2ec4": "西", "\u2ea0": "民", "\u2ed8": "青", "\u2edd": "食", "\u2ee3": "骨",
}


def _fix_radicals(text: str) -> str:
    # NFKC 正規化：Unicode 定義好的「相容字元 → 標準字元」對應，把部首字元換回一般漢字
    # 只對這兩個區段做，不對整段文字做：整段做 NFKC 會連全形標點（，：）都換成半形，改變原文的樣子
    def fix(match: re.Match) -> str:
        ch = match.group()
        return _SUPPLEMENT.get(ch) or unicodedata.normalize("NFKC", ch)

    return _RADICALS.sub(fix, text)


class UnreadablePdf(Exception):
    pass


class TooManyPages(Exception):
    pass


# 何時呼叫：document_service 把 S3 抓回來的 PDF 內容丟進來解析；測試裡也會直接傳本機路徑
# source: str | Path | BinaryIO 代表可以是本機路徑，也可以是 io.BytesIO 這種「檔案物件」，
# PdfReader 兩種都吃，不用自己先判斷是哪一種再分開寫
# max_pages：超過就直接丟 TooManyPages，在抽文字之前先擋，不用白白把上千頁都讀完。上限多少由呼叫的人決定
def extract_text(source: str | Path | BinaryIO, max_pages: int | None = None) -> str:
    try:
        reader = PdfReader(source) # 吃一個檔案路徑或檔案物件 回傳一個「PDF 讀取器」物件(它會自己打開、解析 PDF 內部結構)
        if max_pages is not None and len(reader.pages) > max_pages:
            raise TooManyPages
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "") # 把每一頁的文字抽取出來或是用空字串代替(避免None)
    # as e：把 pypdf 丟出來的錯誤物件存起來，才能在下面用 str(e) 拿到它的訊息內容
    except PdfReadError as e:
        # 把 pypdf 的錯誤「翻譯」成我們自己的 UnreadablePdf 這樣我們的程式不用在乎他的底層究竟是出甚麼錯
        # str(e) 保留原始錯誤訊息文字；from e 保留原始錯誤在 traceback 裡的完整鏈，方便之後除錯
        raise UnreadablePdf(str(e)) from e
    return _fix_radicals("\n".join(pages).strip())
