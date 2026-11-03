from fastapi import APIRouter, HTTPException

from api.dependencies import workflow_engine
from core.orchestrator import SessionDeleteResult

router = APIRouter(tags=["Sessions"])


@router.get("/api/sessions")
async def list_sessions():
    return {"sessions": workflow_engine.list_saved_sessions()}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """直接删除历史记录（无密码控制）"""
    deleted = workflow_engine.delete_session(session_id)
    if deleted == SessionDeleteResult.ACTIVE:
        raise HTTPException(409, "Session is active and cannot be deleted")
    if deleted != SessionDeleteResult.DELETED:
        raise HTTPException(404, "Session not found")
    return {"status": "deleted", "session_id": session_id}


@router.delete("/api/sessions")
async def cleanup_orphan_files():
    """清理孤立的结果文件（无密码控制）"""
    return workflow_engine.cleanup_orphan_results()
