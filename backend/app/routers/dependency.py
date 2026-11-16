"""依赖解析接口：维护依赖关系，覆盖提交关系评审、生效关系、解除关系等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.access import Actor, current_actor
from app.services.dependency import DependencyService

router = APIRouter(prefix="/api/dependency", tags=["依赖解析"])

service = DependencyService()

LIST_FIELDS = ["关系编号", "上游作业", "下游作业", "依赖类型", "偏移量分钟", "触发条件", "等待超时", "跳过标志"]
STATUSES = ["草稿", "关系评审中", "已生效", "已解除"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按关系编号检索"),
    status: str | None = Query(default=None, description="草稿、关系评审中、已生效、已解除"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按关系编号与状态过滤依赖解析列表；重复登记的关系只显示一条，列表与详情上下游一致。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/assignments", response_model=dict)
def list_assignments(actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """关系共享授权清单：观察者只看到共享给自己的只读关系。"""

    return {"items": service.assignments(actor=actor), "total": len(service.assignments(actor=actor))}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出依赖解析清单：返回当前过滤条件下的全量数据，重复关系只出现一次。"""
    items, total = service.list_export()
    return {"module": "dependency", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条依赖关系明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"依赖关系 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(current_actor)) -> ActionResult:
    """登记一条依赖关系；重复登记只保留一条活动边，缺字段或越权时说明原因。"""
    entry, missing, message = service.create_entry(payload.values, actor=actor)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=message or "依赖关系登记失败")
    return ActionResult(ok=True, message=message or "依赖关系已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    actor: Actor = Depends(current_actor),
) -> ActionResult:
    """执行提交关系评审、生效关系、解除关系；观察者只读，跨项目改动会被拒绝。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, actor=actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/share", response_model=ActionResult)
def share_entry(
    entry_id: int,
    payload: EntryPayload,
    actor: Actor = Depends(current_actor),
) -> ActionResult:
    """把关系共享给项目/观察者；观察者只读，不能调整授权。"""

    assignment, message = service.share_relation(
        entry_id,
        project=payload.values.get("项目") or payload.values.get("所属项目"),
        observer=payload.values.get("观察者"),
        maintainer=payload.values.get("维护者"),
        actor=actor,
    )
    if assignment is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=assignment)
