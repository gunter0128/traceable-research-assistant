# 把長文字切成一段一段，準備給 embedding 用。純函式 不碰 DB 不碰 HTTP，跟 pdf_parser.py 同一種角色

import tiktoken

# 跟之後 embedding 要用的模型對齊：text-embedding-3-small
ENCODING = tiktoken.encoding_for_model("text-embedding-3-small")


# 何時呼叫：document_service 抓完 PDF 文字之後，切段存進 chunks 表之前
# chunk_size：每一段最多幾個 token；overlap：段落之間重疊幾個 token（避免關鍵字剛好卡在切割點被切斷）
def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    tokens = ENCODING.encode(text)

    chunks = []
    start = 0
    # 每次往前推進 (chunk_size - overlap) 個 token，讓下一段跟上一段重疊 overlap 個 token
    step = chunk_size - overlap
    while start < len(tokens):
        piece = tokens[start : start + chunk_size]
        # errors="ignore"：中文這種多 byte 字元 切割點可能剛好落在同一個字的 token 中間
        # 切壞的那半個字沒辦法組成合法的 UTF-8 直接丟掉（預設 "replace" 會留下 � 這種亂碼字元）
        # 因為段落之間有 overlap，被丟掉的字通常在鄰近那段是完整的，不會真的遺失內容
        chunks.append(ENCODING.decode(piece, errors="ignore"))
        start += step

    return chunks
