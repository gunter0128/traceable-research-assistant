# 測試共用：用 fpdf2 產生一份「真的」PDF（不是隨便塞幾個 byte 假裝），
# 這樣 pdf_parser 才能真的解析出文字，process_document() 才不會一律失敗
# 這個檔案不是 fixture（不是 @pytest.fixture），是給各個 test_*.py 自己 import 的一般函式

from uuid import uuid4

from fpdf import FPDF


# 內建字型（Helvetica）只支援英數字 不支援中文（要中文得額外掛 Unicode 字型檔）——測試裡要請用英文
def valid_pdf_bytes(text: str = "test content") -> bytes:
    pdf = FPDF()
    # 每次產生的檔案都不一樣（隨機的標題），不會被當成重複文件（docs/decisions/022）
    # 標題是 metadata，不影響抽出來的文字。要測重複文件的話，自己把同一份 bytes 傳兩次
    pdf.set_title(uuid4().hex)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text=text)
    return bytes(pdf.output())


# 一頁空白、沒有任何文字的 PDF：模擬掃描檔（整頁都是圖片，抓不到字）
def blank_pdf_bytes() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    return bytes(pdf.output())
