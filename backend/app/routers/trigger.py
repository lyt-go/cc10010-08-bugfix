"""下游触发接口：接收上游完成事件，生成唯一的下游触发数据记录并回写执行模块。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.access import Actor, current_actor
from app.services.trigger import TriggerService

router = APIRouter(prefix="/api/trigger", tags=["下游触发"])

service = TriggerService()


@router.post("/events", response_model=ActionResult)
def submit_event(payload: EntryPayload) -> ActionResult:
    """登记一个上游完成事件：合法事件生成唯一下游触发记录，重复/迟到事件明确拒绝。"""

    outcome = service.submit_event(payload.values)
    return ActionResult(
        ok=bool(outcome.get("accepted")),
        message=str(outcome.get("message") or ""),
        entry={
            "trigger": outcome.get("trigger"),
            "execution": outcome.get("execution"),
            "duplicate": outcome.get("duplicate", False),
        },
    )


@router.post("/events/batch", response_model=ActionResult)
def submit_events(payload: EntryPayload) -> ActionResult:
    """批量登记上游完成事件：按完成时间顺序处理，只有首个合法事件生成下游触发记录。"""

    raw_events = payload.values.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        return ActionResult(ok=False, message="请提供至少一个上游完成事件（events）")
    outcomes = service.submit_batch([item for item in raw_events if isinstance(item, dict)])
    accepted = sum(1 for outcome in outcomes if outcome.get("accepted"))
    return ActionResult(
        ok=accepted > 0,
        message=f"共处理 {len(outcomes)} 个事件，生成 {accepted} 条下游触发记录",
        entry={"items": outcomes, "accepted": accepted},
    )


@router.get("/events", response_model=dict)
def list_events() -> dict[str, Any]:
    """上游完成事件处理日志：迟到、重复、拒绝的事件都保留可追溯。"""

    items = service.list_events()
    return {"items": items, "total": len(items)}


@router.get("", response_model=PageResult[dict])
def list_triggers(
    relation_no: str | None = Query(default=None, description="按关系编号检索"),
    downstream: str | None = Query(default=None, description="按下游作业检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """唯一的下游触发数据记录清单：一条记录只对应一次上游事件并保留关系版本。"""

    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_triggers(relation_no=relation_no, downstream=downstream, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/execution", response_model=dict)
def list_execution() -> dict[str, Any]:
    """执行模块清单：只按唯一的触发记录追加执行项，结果与触发记录一一对应。"""

    return {
        "items": service.list_execution(),
        "results": service.list_results(),
        "business_status": service.list_business_status(),
    }


@router.post("/execution/{item_id}/actions", response_model=ActionResult)
def run_execution_action(item_id: int, payload: EntryPayload) -> ActionResult:
    """执行模块按唯一触发记录推进状态；关系版本变化后旧执行项被拒绝。"""

    action = str(payload.values.get("action") or "").strip()
    item, message = service.run_execution_action(item_id, action)
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


@router.get("/{trigger_id}", response_model=dict)
def get_trigger(trigger_id: int) -> dict:
    """读取单条下游触发记录及其执行回写结果。"""

    trigger = service.get_trigger(trigger_id)
    if trigger is None:
        raise HTTPException(status_code=404, detail=f"下游触发记录 {trigger_id} 不存在或已归档")
    return trigger
