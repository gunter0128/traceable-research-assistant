# 把長文字切成一段一段，準備給 embedding 用。純函式 不碰 DB 不碰 HTTP，跟 pdf_parser.py 同一種角色

import re
from itertools import pairwise

import tiktoken

# 跟之後 embedding 要用的模型對齊：text-embedding-3-small
ENCODING = tiktoken.encoding_for_model("text-embedding-3-small")

# 每一段最多幾個 token
CHUNK_SIZE = 500

# 切段的方式（docs/decisions/032）
#   fixed：固定長度硬切（現在線上用的）
#   structured：照文件的結構切，不把一條規定、一句話從中間切斷，每段開頭附上它在文件裡的位置
# 線上要不要換，看評估結果決定
STRATEGY = "fixed"


# 何時呼叫：document_service 抓完 PDF 文字之後，切段存進 chunks 表之前
def chunk(text: str) -> list[str]:
    if STRATEGY == "structured":
        return chunk_structured(text, CHUNK_SIZE)
    return chunk_text(text, CHUNK_SIZE)


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


# ---------- 照文件結構切（docs/decisions/032）----------
# 原則：每一段都要「單獨看得懂」。固定長度硬切會把一條規定、一句話切成兩半（例如特休的天數表跟前面的說明分開），
# 搜尋時兩半都不像問題，回答時也只看到一半
#
# 做法：一層一層找文件本身的結構，先照最大的結構切，切出來還太長的那一塊，再用下一層切
# 認的是常見的編號寫法，不是特定文件的格式：法規、公司規章、學校要點、有編號的論文都適用；
# 完全沒有這些結構的文字，會退到句號，最後才硬切，所以不會比固定長度差
_NUMBER = r"[0-9一二三四五六七八九十百零〇]+"
LEVELS = [
    re.compile(rf"^第\s*{_NUMBER}\s*[編章]"),  # 第 一 章、第一章
    re.compile(rf"^第\s*{_NUMBER}\s*節"),  # 第 一 節
    re.compile(rf"^第\s*{_NUMBER}(?:-\d+)?\s*[條點]"),  # 第 38 條、第 9-1 條、第三點
    re.compile(r"^(?:[一二三四五六七八九十]+、|\d+(?:\.\d+)*[\.、]?\s)"),  # 一、　1.　2.1
    re.compile(r"^[（(][一二三四五六七八九十0-9]+[）)]"),  # （一）、(1)
]
# 第 1～3 層是「標題」：切出來的段落如果是某一條的後半部，開頭要附上它屬於哪一章、哪一條
HEADING_LEVELS = 3


def _tokens(text: str) -> int:
    return len(ENCODING.encode(text))


# 把一行一行的文字，切成一個個「單位」：(文字, 這個單位屬於哪些標題)
def _units(lines: list[str], level: int, context: list[str], limit: int) -> list[tuple[str, list[str]]]:
    text = "\n".join(lines)
    if _tokens(text) <= limit:
        return [(text, context)]
    if level == len(LEVELS):
        return [(piece, context) for piece in _split_sentences(text, limit)]
    starts = [i for i, line in enumerate(lines) if LEVELS[level].match(line.strip())]
    if not starts:
        return _units(lines, level + 1, context, limit)
    # 第一個標題之前的文字（例如文件開頭、章名底下的說明）自成一塊
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(lines)]
    units = []
    for begin, end in pairwise(bounds):
        group = lines[begin:end]
        if _tokens("\n".join(group)) <= limit:
            # 整塊放得下（例如一條完整的條文）：位置跟隔壁一樣，可以跟隔壁合併
            units.append(("\n".join(group), context))
            continue
        # 放不下、要再拆的：拆出來的每一塊都記下它屬於哪一章、哪一條
        heading = group[0].strip() if begin in starts and level < HEADING_LEVELS else None
        units += _units(group, level + 1, context + ([heading] if heading else []), limit)
    return units


# 沒有任何結構可以切的長文字：在句號後面切，一句話還是太長才硬切
def _split_sentences(text: str, limit: int) -> list[str]:
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[。！？；.!?])", text):
        if current and _tokens(current + sentence) > limit:
            pieces.append(current)
            current = ""
        while _tokens(sentence) > limit:
            tokens = ENCODING.encode(sentence)
            pieces.append(ENCODING.decode(tokens[:limit], errors="ignore"))
            sentence = ENCODING.decode(tokens[limit:], errors="ignore")
        current += sentence
    return [p for p in pieces + [current] if p.strip()]


def chunk_structured(text: str, chunk_size: int = 500) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    title = lines[0].strip()
    # 位置的前綴要佔一些 token：先留 60 個給它，切出來的單位再小一點
    units = _units(lines, 0, [], chunk_size - 60)

    # 相鄰的小單位合在一起，湊到上限以內
    # 只合併「位置一樣」的單位：完整的小條文可以互相合併；被拆開的長條文，它的幾塊只跟自己合併，
    # 不會變成「上一條 + 這一條的前半」這種兩邊都不完整的段落
    chunks, current, current_context = [], [], []
    for text_, context in units:
        too_big = _tokens("\n".join(current + [text_])) > chunk_size - 60
        if current and (too_big or context != current_context):
            chunks.append((current, current_context))
            current = []
        if not current:
            current_context = context
        current.append(text_)
    if current:
        chunks.append((current, current_context))

    results = []
    for index, (parts, context) in enumerate(chunks):
        body = "\n".join(parts)
        # 第一段本來就從文件標題開始，不用再加
        path = [] if index == 0 else [title, *[h for h in context if not body.startswith(h)]]
        results.append(f"【{' > '.join(path)}】\n{body}" if path else body)
    return results
