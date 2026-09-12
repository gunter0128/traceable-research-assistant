# workspace 的規則：ownership 檢查、CRUD 流程
# 不寫 SQL 不碰 HTTP

from sqlalchemy.orm import Session

from app.models.workspace import Workspace
from app.repositories import workspace_repo


# 一樣是個自定義的錯誤類別
class WorkspaceNotFound(Exception):
    pass


class NotWorkspaceOwner(Exception):
    pass


def list_workspaces(db: Session, owner_id: int) -> list[Workspace]:
    return workspace_repo.list_for_owner(db, owner_id)


def create_workspace(db: Session, owner_id: int, name: str) -> Workspace:
    return workspace_repo.create(db, owner_id, name)


def get_owned_workspace(db: Session, workspace_id: int, owner_id: int) -> Workspace:
    # 先查、再檢查是不是你的 之後 update / delete / document 相關功能都重用這個檢查
    # 刻意的介面設計：用參數的型別 逼呼叫者走過必要的步驟
    workspace = workspace_repo.get_by_id(db, workspace_id)
    if workspace is None:
        raise WorkspaceNotFound
    if workspace.owner_id != owner_id:
        raise NotWorkspaceOwner
    return workspace


def update_workspace(
    db: Session, workspace_id: int, owner_id: int, name: str
) -> Workspace:
    workspace = get_owned_workspace(db, workspace_id, owner_id)
    return workspace_repo.update_name(db, workspace, name)


def delete_workspace(db: Session, workspace_id: int, owner_id: int) -> None:
    workspace = get_owned_workspace(db, workspace_id, owner_id)
    workspace_repo.delete(db, workspace)
