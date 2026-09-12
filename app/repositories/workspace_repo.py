# workspaces 表的資料庫存取。只跟 DB 講話 不放商業邏輯

from sqlalchemy.orm import Session

from app.models.workspace import Workspace


# SELECT * FROM workspaces WHERE owner_id = ?
def list_for_owner(db: Session, owner_id: int) -> list[Workspace]:
    return db.query(Workspace).filter(Workspace.owner_id == owner_id).all()


def get_by_id(db: Session, workspace_id: int) -> Workspace | None:
    return db.get(Workspace, workspace_id)


def create(db: Session, owner_id: int, name: str) -> Workspace:
    workspace = Workspace(owner_id=owner_id, name=name)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


# 這時候 user 手上已經有撈出來的物件了 所以直接對物件動手
def update_name(db: Session, workspace: Workspace, name: str) -> Workspace:
    workspace.name = name
    db.commit()
    db.refresh(workspace)
    return workspace


# 這時候 user 手上已經有撈出來的物件了 所以直接對物件動手
def delete(db: Session, workspace: Workspace) -> None:
    db.delete(workspace)
    db.commit()
