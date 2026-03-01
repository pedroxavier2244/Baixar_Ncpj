from fastapi import APIRouter
from api.schemas import RunRecord
from db.control import list_runs

router = APIRouter()


@router.get("", response_model=list[RunRecord])
def get_runs(limit: int = 10):
    limit = min(max(1, limit), 50)
    rows = list_runs(limit=limit)
    return [RunRecord(**dict(r)) for r in rows]
