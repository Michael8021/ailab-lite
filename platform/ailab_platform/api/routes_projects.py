"""Projects group conversations and library files."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..library import delete_file_rows, delete_storage_objects, owned_project
from ..locks import user_critical_section
from ..models import Conversation, FileObject, Project, Run, RunStatus, User, utcnow
from ..run_state import transition_run
from ..schemas import ProjectIn, ProjectOut

router = APIRouter(prefix="/v1/projects", tags=["projects"])


def _project_rows(db: Session, user_id: str, project_id: str | None = None) -> list[dict]:
    conversations = (
        select(Conversation.project_id, func.count().label("count"))
        .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
        .where(Conversation.project_id == project_id if project_id is not None else Conversation.project_id.is_not(None))
        .group_by(Conversation.project_id)
        .subquery()
    )
    files = (
        select(FileObject.project_id, func.count().label("count"))
        .where(FileObject.user_id == user_id)
        .where(FileObject.project_id == project_id if project_id is not None else FileObject.project_id.is_not(None))
        .group_by(FileObject.project_id)
        .subquery()
    )
    stmt = (
        select(Project, func.coalesce(conversations.c.count, 0), func.coalesce(files.c.count, 0))
        .outerjoin(conversations, conversations.c.project_id == Project.id)
        .outerjoin(files, files.c.project_id == Project.id)
        .where(Project.user_id == user_id)
        .order_by(Project.updated_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    return [
        {
            "id": project.id,
            "name": project.name,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "conversations": conversation_count,
            "files": file_count,
        }
        for project, conversation_count, file_count in db.execute(stmt)
    ]


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return _project_rows(db, user.id)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    project = Project(user_id=user.id, name=body.name)
    db.add(project)
    db.commit()
    return _project_rows(db, user.id, project.id)[0]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    owned_project(db, project_id, user)
    return _project_rows(db, user.id, project_id)[0]


@router.patch("/{project_id}", response_model=ProjectOut)
def rename_project(project_id: str, body: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    with user_critical_section(db, user.id):
        project = owned_project(db, project_id, user)
        project.name = body.name
        project.updated_at = utcnow()
        db.commit()
        return _project_rows(db, user.id, project_id)[0]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    with user_critical_section(db, user.id):
        project = owned_project(db, project_id, user)
        conversation_ids = select(Conversation.id).where(Conversation.project_id == project.id)
        runs = db.scalars(select(Run).where(Run.conversation_id.in_(conversation_ids), Run.status.in_(RunStatus.ACTIVE))).all()
        for run in runs:
            transition_run(db, run.id, RunStatus.CANCELED, reason="project_deleted", commit=False)
        # Retain the soft-deleted threads and their run history without a dangling project FK.
        db.execute(update(Conversation).where(Conversation.project_id == project.id).values(project_id=None, deleted_at=utcnow()))
        files = list(db.scalars(select(FileObject).where(FileObject.project_id == project.id)))
        keys = delete_file_rows(db, files, detach_children=True)
        db.delete(project)
        db.commit()
    delete_storage_objects(keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
