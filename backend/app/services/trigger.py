"""下游触发业务规则：上游完成事件校验、去重与唯一的下游触发数据记录。

处理流程：

1. 校验事件必填字段，上游作业不能等于下游作业（依赖自己直接被拒绝）。
2. 定位事件指向的关系版本：优先按关系编号 + 关系版本，其次按上下游方向找当前活动关系。
3. 按事件完成时间顺序处理事件队列；迟到事件、重复事件、指向已解除关系的事件、
   版本冲突的事件都不会生成新的下游触发记录，只记录明确的拒绝原因。
4. 同一上游完成事件最多生成一条下游触发记录（以事件编号唯一约束）；
   ``(关系, 关系版本, 事件编号)`` 与 ``(关系, 关系版本, 业务日期)`` 都不允许重复。
5. 下游触发记录回写到执行模块清单与执行结果，重复事件不追加执行项。

「旧关系 + 新关系」多个完成事件冲突时，按当前关系版本过滤：
关系重新生效会推进版本号，旧版本事件在新版本生效后到达时被版本校验拒绝，
不会触发下游；未显式带版本的事件按上下游方向定位到当前活动版本，
若其完成时间早于该版本已处理的最新事件，则按迟到事件拒绝。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.dependency import (
    ACTIVE_STATUS,
    DependencyService,
    _is_active,
    _text,
    _version,
    relation_identity,
)
from app.store import store, store_lock

TRIGGER_TABLE = "downstream_trigger"
EVENT_TABLE = "upstream_event"
EXECUTION_TABLE = "execution_item"
RESULT_TABLE = "execution_result"
STATUS_TABLE = "execution_status"

ACCEPTED_RESULT = "已生成下游触发记录"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _event_key(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(payload.get("事件编号")),
        _text(payload.get("上游作业")),
        _text(payload.get("下游作业")),
        _text(payload.get("业务日期")),
    )


class TriggerService:
    def __init__(self) -> None:
        self.dependency = DependencyService()

    # ------------------------------------------------------------------ 关系定位
    def _resolve_relation(
        self,
        payload: dict[str, Any],
        completed_at: datetime | None,
    ) -> tuple[dict[str, Any] | None, int, str]:
        """按事件内容定位关系与版本；返回 ``(关系, 版本, 错误说明)``。"""

        relation_no = _text(payload.get("关系编号"))
        requested_version = payload.get("关系版本")
        upstream = _text(payload.get("上游作业"))
        downstream = _text(payload.get("下游作业"))

        candidates = [
            row for row in store.rows("dependency")
            if (not relation_no or _text(row.get("关系编号")) == relation_no)
            and _text(row.get("上游作业")) == upstream
            and _text(row.get("下游作业")) == downstream
        ]
        if not candidates:
            return None, 0, f"上游作业 {upstream} 到下游作业 {downstream} 没有匹配的依赖关系"

        latest = max(candidates, key=lambda row: (_version(row), _is_active(row), int(row.get("id", 0))))
        current_version = _version(latest)

        try:
            requested = int(requested_version) if requested_version not in (None, "") else None
        except (TypeError, ValueError):
            return None, current_version, f"关系版本「{requested_version}」不是合法版本号"

        if requested is not None and requested != current_version:
            return None, current_version, (
                f"关系版本校验失败：事件指向版本 {requested}，当前关系版本为 {current_version}，"
                "不写入新的下游触发记录"
            )

        if not _is_active(latest) or latest.get("status") != ACTIVE_STATUS:
            return None, current_version, (
                f"依赖关系 {latest.get('关系编号')} 状态为「{latest.get('status')}」，"
                "事件指向已解除的关系，不参与下游触发"
            )

        return latest, current_version, ""

    # ------------------------------------------------------------------ 事件登记
    def submit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记一个上游完成事件；返回处理结果（是否生成下游触发记录、拒绝原因等）。

        校验、去重、写触发记录与回写执行模块在同一把锁内完成，并发重复请求只有一个成功。
        """

        with store_lock:
            return self._submit_event_locked(payload)

    def _submit_event_locked(self, payload: dict[str, Any]) -> dict[str, Any]:

        event_id = _text(payload.get("事件编号"))
        upstream = _text(payload.get("上游作业"))
        downstream = _text(payload.get("下游作业"))
        if not event_id or not upstream or not downstream:
            return self._reject(payload, "缺少必填字段：事件编号、上游作业、下游作业")
        if upstream == downstream:
            return self._reject(payload, f"上游作业「{upstream}」不能依赖自己，事件被拒绝")

        completed_at = _parse_time(payload.get("完成时间"))
        if completed_at is None:
            return self._reject(payload, "完成时间缺失或不是合法时间，事件被拒绝")

        existing_event = self._find_event(event_key=_event_key(payload))
        if existing_event is not None:
            reason = (
                f"上游完成事件 {event_id} 已处理（{existing_event.get('result')}），"
                "重复请求被拒绝，不重复写入下游触发结果"
            )
            self._record_event(payload, completed_at, result="重复事件", reason=reason,
                               trigger_id=existing_event.get("trigger_id"))
            return {
                "ok": False,
                "accepted": False,
                "duplicate": True,
                "message": reason,
                "trigger": self._trigger_by_id(existing_event.get("trigger_id")),
                "event": payload,
            }

        relation, version, error = self._resolve_relation(payload, completed_at)
        if relation is None:
            self._record_event(payload, completed_at, result="已拒绝", reason=error)
            return {"ok": False, "accepted": False, "duplicate": False,
                    "message": error, "trigger": None, "event": payload}

        # 迟到事件：完成时间早于该关系版本已处理的最新事件完成时间，按当前边界过滤。
        latest_completed = self._latest_event_time(_text(relation.get("关系编号")), version)
        if latest_completed is not None and completed_at < latest_completed:
            reason = (
                f"迟到事件：完成时间 {completed_at.isoformat()} 早于关系版本 {version} 已处理的最新事件 "
                f"{latest_completed.isoformat()}，不生成下游触发记录"
            )
            self._record_event(payload, completed_at, result="迟到事件", reason=reason)
            return {"ok": False, "accepted": False, "late": True,
                    "message": reason, "trigger": None, "event": payload}

        business_date = _text(payload.get("业务日期")) or completed_at.date().isoformat()
        conflict = self._find_trigger(
            relation_no=_text(relation.get("关系编号")),
            version=version,
            event_id=event_id,
            business_date=business_date,
        )
        if conflict is not None:
            reason = (
                f"同一上游完成事件（{event_id}）已生成下游触发记录 {conflict.get('触发编号')}，"
                "请求被拒绝，不重复写入下游触发结果"
            )
            self._record_event(payload, completed_at, result="重复事件", reason=reason,
                               trigger_id=conflict.get("id"))
            return {"ok": False, "accepted": False, "duplicate": True, "message": reason,
                    "trigger": conflict, "event": payload}

        trigger = self._create_trigger(
            payload=payload,
            relation=relation,
            version=version,
            business_date=business_date,
        )
        self._record_event(payload, completed_at, result=ACCEPTED_RESULT, reason="",
                           trigger_id=trigger["id"])
        return {
            "ok": True,
            "accepted": True,
            "duplicate": False,
            "message": "已生成唯一的下游触发数据记录",
            "trigger": trigger,
            "execution": self.trigger_execution(trigger),
            "event": payload,
        }

    def submit_batch(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量登记事件：按完成时间顺序处理，保证迟到事件按同一边界过滤。"""

        ordered = sorted(
            enumerate(events),
            key=lambda pair: (
                _parse_time(pair[1].get("完成时间")) or datetime.max.replace(tzinfo=timezone.utc),
                pair[0],
            ),
        )
        return [self.submit_event(payload) for _, payload in ordered]

    # ------------------------------------------------------------------ 执行模块回写
    def trigger_execution(self, trigger: dict[str, Any]) -> dict[str, Any]:
        """按唯一的下游触发记录生成/复用执行模块条目与结果，重复事件不追加。"""

        existing_item = self._find_execution(trigger)
        if existing_item is not None:
            return existing_item

        items = store.rows(EXECUTION_TABLE)
        results = store.rows(RESULT_TABLE)
        statuses = store.rows(STATUS_TABLE)
        sequence = _next_id(items)
        item = {
            "id": sequence,
            "执行项编号": f"EXEC-{sequence:04d}",
            "触发编号": trigger.get("触发编号"),
            "下游触发记录": trigger.get("id"),
            "下游作业": trigger.get("下游作业"),
            "关系编号": trigger.get("关系编号"),
            "关系版本": trigger.get("关系版本"),
            "事件编号": trigger.get("事件编号"),
            "业务日期": trigger.get("业务日期"),
            "状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        items.append(item)
        results.append({
            "id": _next_id(results),
            "下游触发记录": trigger.get("id"),
            "触发编号": trigger.get("触发编号"),
            "执行项编号": item["执行项编号"],
            "执行状态": "待执行",
            "关系版本": trigger.get("关系版本"),
            "事件编号": trigger.get("事件编号"),
            "更新人": "依赖解析",
            "updated_at": _now(),
        })
        statuses.append({
            "id": _next_id(statuses),
            "下游作业": trigger.get("下游作业"),
            "关系编号": trigger.get("关系编号"),
            "关系版本": trigger.get("关系版本"),
            "业务面状态": "待执行",
            "执行状态": "待执行",
            "执行项编号": item["执行项编号"],
            "下游触发记录": trigger.get("id"),
            "updated_at": _now(),
        })
        trigger["执行项编号"] = item["执行项编号"]
        trigger["执行状态"] = "待执行"
        return item

    # ------------------------------------------------------------------ 内部工具
    def _reject(self, payload: dict[str, Any], reason: str) -> dict[str, Any]:
        self._record_event(payload, _parse_time(payload.get("完成时间")), result="已拒绝", reason=reason)
        return {"ok": False, "accepted": False, "duplicate": False,
                "message": reason, "trigger": None, "event": payload}

    def _find_event(self, *, event_key: tuple[str, str, str, str]) -> dict[str, Any] | None:
        for row in store.rows(EVENT_TABLE):
            if _text(row.get("result")) == "已拒绝":
                continue
            if (
                _text(row.get("事件编号")),
                _text(row.get("上游作业")),
                _text(row.get("下游作业")),
                _text(row.get("业务日期")),
            ) == event_key:
                return row
        return None

    def _record_event(
        self,
        payload: dict[str, Any],
        completed_at: datetime | None,
        *,
        result: str,
        reason: str,
        trigger_id: int | None = None,
    ) -> dict[str, Any]:
        rows = store.rows(EVENT_TABLE)
        row = {
            "id": _next_id(rows),
            "事件编号": _text(payload.get("事件编号")),
            "上游作业": _text(payload.get("上游作业")),
            "下游作业": _text(payload.get("下游作业")),
            "关系编号": _text(payload.get("关系编号")),
            "关系版本": payload.get("关系版本"),
            "业务日期": _text(payload.get("业务日期")),
            "完成时间": completed_at.isoformat() if completed_at else _text(payload.get("完成时间")),
            "result": result,
            "reason": reason,
            "trigger_id": trigger_id,
            "received_at": _now(),
        }
        rows.append(row)
        return row

    def _latest_event_time(self, relation_no: str, version: int) -> datetime | None:
        times = [
            _parse_time(row.get("完成时间"))
            for row in store.rows(EVENT_TABLE)
            if _text(row.get("关系编号")) == relation_no
            and str(row.get("关系版本")) == str(version)
            and _text(row.get("result")) == ACCEPTED_RESULT
        ]
        valid = [moment for moment in times if moment is not None]
        return max(valid) if valid else None

    def _find_trigger(
        self,
        *,
        relation_no: str,
        version: int,
        event_id: str,
        business_date: str,
    ) -> dict[str, Any] | None:
        for row in store.rows(TRIGGER_TABLE):
            if _text(row.get("关系编号")) != relation_no:
                continue
            if int(row.get("关系版本") or 1) != version:
                continue
            if _text(row.get("事件编号")) == event_id or _text(row.get("业务日期")) == business_date:
                return row
        return None

    def _create_trigger(
        self,
        *,
        payload: dict[str, Any],
        relation: dict[str, Any],
        version: int,
        business_date: str,
    ) -> dict[str, Any]:
        rows = store.rows(TRIGGER_TABLE)
        sequence = _next_id(rows)
        trigger = {
            "id": sequence,
            "触发编号": f"TRIG-{sequence:04d}",
            "关系编号": _text(relation.get("关系编号")),
            "关系版本": version,
            "identity": list(relation_identity(relation)),
            "事件编号": _text(payload.get("事件编号")),
            "上游作业": _text(relation.get("上游作业")),
            "下游作业": _text(relation.get("下游作业")),
            "方向": f"{_text(relation.get('上游作业'))}->{_text(relation.get('下游作业'))}",
            "业务日期": business_date,
            "完成时间": _text(payload.get("完成时间")),
            "触发时间": _now(),
            "执行状态": "待执行",
        }
        rows.append(trigger)
        item = self.trigger_execution(trigger)
        trigger["执行项编号"] = item.get("执行项编号")
        trigger["执行状态"] = item.get("状态", "待执行")
        return trigger

    def _trigger_by_id(self, trigger_id: object) -> dict[str, Any] | None:
        try:
            target = int(trigger_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        for row in store.rows(TRIGGER_TABLE):
            if int(row.get("id", 0)) == target:
                return row
        return None

    def _find_execution(self, trigger: dict[str, Any]) -> dict[str, Any] | None:
        for row in store.rows(EXECUTION_TABLE):
            if int(row.get("下游触发记录", 0) or 0) == int(trigger.get("id", 0)):
                return row
        return None

    # ------------------------------------------------------------------ 查询
    def list_triggers(
        self,
        *,
        relation_no: str | None = None,
        downstream: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(TRIGGER_TABLE)
        if relation_no:
            rows = [row for row in rows if relation_no in _text(row.get("关系编号"))]
        if downstream:
            rows = [row for row in rows if downstream in _text(row.get("下游作业"))]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)[start:start + size]
        return page_rows, total

    def get_trigger(self, trigger_id: int) -> dict[str, Any] | None:
        return self._trigger_by_id(trigger_id)

    def list_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return list(reversed(store.rows(EVENT_TABLE)))[:limit]

    def list_execution(self) -> list[dict[str, Any]]:
        return sorted(store.rows(EXECUTION_TABLE), key=lambda row: int(row.get("id", 0)))

    def list_results(self) -> list[dict[str, Any]]:
        return sorted(store.rows(RESULT_TABLE), key=lambda row: int(row.get("id", 0)))

    def list_business_status(self) -> list[dict[str, Any]]:
        return sorted(store.rows(STATUS_TABLE), key=lambda row: int(row.get("id", 0)))

    def execution_item(self, item_id: int) -> dict[str, Any] | None:
        for row in store.rows(EXECUTION_TABLE):
            if int(row.get("id", 0)) == item_id:
                return row
        return None

    def run_execution_action(
        self,
        item_id: int,
        action: str,
        *,
        operator: str = "执行模块",
    ) -> tuple[dict[str, Any] | None, str]:
        """执行模块按唯一的触发记录推进执行状态，并同步业务面状态。"""

        with store_lock:
            return self._run_execution_action_locked(item_id, action, operator=operator)

    def _run_execution_action_locked(
        self,
        item_id: int,
        action: str,
        *,
        operator: str = "执行模块",
    ) -> tuple[dict[str, Any] | None, str]:
        actions = {"开始执行": "执行中", "标记完成": "已完成", "标记失败": "失败"}
        item = self.execution_item(item_id)
        if item is None:
            return None, f"执行项 {item_id} 不存在或已归档"
        if action not in actions:
            return None, f"动作「{action}」不属于执行模块可执行范围"
        target = actions[action]

        relation = None
        for row in store.rows("dependency"):
            if _text(row.get("关系编号")) == _text(item.get("关系编号")):
                relation = row
                break
        current_version = _version(relation) if relation is not None else int(item.get("关系版本") or 1)
        if current_version != int(item.get("关系版本") or 1):
            return None, (
                f"关系版本已更新为 {current_version}，执行项 {item.get('执行项编号')} 仍绑定版本 "
                f"{item.get('关系版本')}，请按新版本重新生成触发记录"
            )
        if relation is not None and not _is_active(relation):
            return None, f"依赖关系 {item.get('关系编号')} 已不再生效，执行项不能被推进"

        item["状态"] = target
        item["status"] = target
        item["pending"] = target not in ("已完成", "失败")
        item["abnormal"] = target == "失败"

        for result in store.rows(RESULT_TABLE):
            if int(result.get("下游触发记录", 0) or 0) == int(item.get("下游触发记录", 0)):
                result["执行状态"] = target
                result["更新人"] = operator
                result["updated_at"] = _now()
        for trigger in store.rows(TRIGGER_TABLE):
            if int(trigger.get("id", 0)) == int(item.get("下游触发记录", 0)):
                trigger["执行状态"] = target
        for status in store.rows(STATUS_TABLE):
            if int(status.get("下游触发记录", 0) or 0) == int(item.get("下游触发记录", 0)):
                status["业务面状态"] = target
                status["执行状态"] = target
                status["关系版本"] = current_version
                status["updated_at"] = _now()
        return item, f"执行项已{action}"
