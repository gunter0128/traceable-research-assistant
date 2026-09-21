# 測試共用：手刻一份「真的」PDF（不是隨便塞幾個 byte 假裝），
# 這樣 pdf_parser 才能真的解析出文字，process_document() 才不會一律失敗
# 這個檔案不是 fixture（不是 @pytest.fixture），是給各個 test_*.py 自己 import 的一般函式

import io


def valid_pdf_bytes(text: str = "test content") -> bytes:
    # 內容流手刻成 latin-1，只支援英數字，不支援中文（要中文得幫字型做 Unicode CMap，
    # 對測試用的 fixture 太複雜了）——測試裡要放可辨識文字，請用英文
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    }

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode())
        buf.write(objects[num])
        buf.write(b"\nendobj\n")

    xref_offset = buf.tell()
    n = len(objects) + 1
    buf.write(f"xref\n0 {n}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for num in sorted(objects):
        buf.write(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())

    return buf.getvalue()
