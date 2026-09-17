# 把所有 model 都 import 進來 讓 SQLAlchemy / Alembic 一次找得到全部
# 不然新建的表如果沒有被 import 就不會存到 Base


from app.models.chunk import Chunk
from app.models.document import Document
from app.models.user import User
from app.models.workspace import Workspace

__all__ = ["Chunk", "Document", "User", "Workspace"]
