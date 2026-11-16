"""依赖解析业务规则：关系身份去重、版本流转与状态过滤都收在这里。

关键口径：

- 关系身份 = 关系编号 + 上游作业 + 下游作业 + 方向 + 状态。同一身份只保留一条活动边，
  重复登记返回既有记录，不新增行。
- 关系版本（``relation_version``）在生效时递增；解除后再生效会得到新的版本号，
  旧版本的完成事件不能作用于新版本。
- 列表与详情共用同一份「上下游对」，重复关系不会在列表里重复显示。
- 维护权限按项目归属判定：上游/下游跨项目改动被拒绝，观察者只能读取共享关系。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.access import Actor, validate_project_scope
from app.store import store, store_lock

MODULE = "dependency"
REQUIRED_FIELDS = ["关系编号", "上游作业", "下游作业"]
STATUS_ORDER = ["草稿", "关系评审中", "已生效", "已解除"]
ACTION_RULES = {"提交关系评审": "关系评审中", "生效关系": "已生效", "解除关系": "已解除"}
# 每个动作允许的来源状态：已生效不能直接重新评审，必须先解除再按新版本生效。
ACTION_SOURCES = {
    "提交关系评审": {"草稿", "已解除"},
    "生效关系": {"草稿", "关系评审中", "已解除"},
    "解除关系": {"草稿", "关系评审中", "已生效"},
}
NEGATIVE_ACTIONS = []

ACTIVE_STATUS = "已生效"
RELEASED_STATUS = "已解除"

# 展示字段：列表、详情、导出共用，保证「列表里的上下游」与「详情里的上下游」一致。
DETAIL_FIELDS = [
    "关系编号",
    "上游作业",
    "下游作业",
    "依赖类型",
    "偏移量分钟",
    "触发条件",
    "等待超时",
    "跳过标志",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object) -> str:
    return str(value or "").strip()


def _version(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("relation_version") or 1)
    except (TypeError, ValueError):
        return 1


def _direction(entry: dict[str, Any]) -> tuple[str, str]:
    """关系方向：上游 -> 下游。方向参数与字段顺序一起参与身份判定。"""

    return _text(entry.get("上游作业")), _text(entry.get("下游作业"))


def relation_identity(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """关系身份：关系编号、上游作业、下游作业、方向、状态共同参与去重。"""

    upstream, downstream = _direction(entry)
    return (
        _text(entry.get("关系编号")),
        upstream,
        downstream,
        f"{upstream}->{downstream}",
        _text(entry.get("status")),
    )


def _is_active(entry: dict[str, Any]) -> bool:
    return entry.get("status") == ACTIVE_STATUS and bool(entry.get("active", True))


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """详情结构：显式带上 identity/version/active，列表与详情都从同一结构派生。"""

    payload = {field: entry.get(field) for field in DETAIL_FIELDS}
    payload.update({
        "upstream": _text(entry.get("上游作业")),
        "downstream": _text(entry.get("下游作业")),
        "direction": f"{_text(entry.get('上游作业'))}->{_text(entry.get('下游作业'))}",
        "identity": list(relation_identity(entry)),
        "relation_version": _version(entry),
        "版本": _version(entry),
        "active": _is_active(entry),
        "所属项目": _text(entry.get("所属项目")) or None,
        "project": _text(entry.get("所属项目")) or None,
        "version_started_at": entry.get("version_started_at"),
        "deactivated_at": entry.get("deactivated_at"),
        "status": entry.get("status"),
        "pending": entry.get("pending"),
        "abnormal": entry.get("abnormal"),
        "id": entry.get("id"),
    })
    return payload


class DependencyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("关系编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 去重：身份相同的重复登记只保留 id 最小（即最早登记）的那一条，列表不再重复显示。
        deduped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in sorted(rows, key=lambda item: int(item.get("id", 0))):
            deduped.setdefault(relation_identity(row), row)
        ordered = sorted(deduped.values(), key=lambda item: int(item.get("id", 0)))
        total = len(ordered)
        start = max(page - 1, 0) * size
        return [_public(row) for row in ordered[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return _public(entry) if entry is not None else None

    def list_export(self) -> tuple[list[dict[str, Any]], int]:
        return self.list_entries(page=1, size=10000)

    def find_active(
        self,
        *,
        upstream: str,
        downstream: str,
        relation_no: str | None = None,
    ) -> dict[str, Any] | None:
        """按方向查找当前活动关系；同一对上下游有多条历史行时取最早的活动边。"""

        candidates = [
            row for row in store.rows(MODULE)
            if _is_active(row)
            and _direction(row) == (upstream, downstream)
            and (relation_no is None or _text(row.get("关系编号")) == relation_no)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda row: int(row.get("id", 0)))

    def _find_identity(self, values: dict[str, Any]) -> dict[str, Any] | None:
        target = relation_identity({
            "关系编号": values.get("关系编号"),
            "上游作业": values.get("上游作业"),
            "下游作业": values.get("下游作业"),
            "status": values.get("status") or STATUS_ORDER[0],
        })
        for row in store.rows(MODULE):
            if relation_identity(row) == target:
                return row
        return None

    def create_entry(
        self,
        values: dict[str, Any],
        *,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        """登记依赖关系。

        返回 ``(entry, missing, message)``：``entry`` 为既有或新登记的关系，
        ``missing`` 非空表示缺字段。重复登记不新增行，直接返回已有记录。
        """

        with store_lock:
            return self._create_entry_locked(values, actor=actor)

    def _create_entry_locked(
        self,
        values: dict[str, Any],
        *,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing, ""

        if actor is not None and not actor.can_write:
            return None, [], "观察者只读共享关系，不能登记或改动依赖关系"

        upstream = _text(values.get("上游作业"))
        downstream = _text(values.get("下游作业"))
        if upstream == downstream:
            return None, [], f"作业「{upstream}」不能依赖自己，请换一个下游作业"
        relation_no = _text(values.get("关系编号"))

        scope_error = validate_project_scope(
            upstream=upstream,
            downstream=downstream,
            upstream_project=values.get("上游项目"),
            downstream_project=values.get("下游项目"),
            project=values.get("所属项目"),
            actor=actor,
        )
        if scope_error:
            return None, [], scope_error

        existing = self._find_identity(values)
        if existing is not None:
            return _public(existing), [], f"依赖关系 {relation_no} 已登记，返回既有记录"

        # 同一对上下游、同一方向已经存在活动边：视为重复登记，只保留一条活动边。
        active = self.find_active(upstream=upstream, downstream=downstream)
        if active is not None:
            return _public(active), [], "同一上下游的活动依赖关系已存在，未重复登记"

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        }
        for field in DETAIL_FIELDS:
            entry[field] = values.get(field)
        entry["关系编号"] = relation_no
        entry["上游作业"] = upstream
        entry["下游作业"] = downstream
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["relation_version"] = 1
        entry["version_started_at"] = _now()
        entry["active"] = False
        entry["deactivated_at"] = None
        entry["所属项目"] = _text(values.get("所属项目")) or None
        rows.append(entry)
        return _public(entry), [], "依赖关系已登记"

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with store_lock:
            return self._run_action_locked(entry_id, action, actor=actor)

    def _run_action_locked(
        self,
        entry_id: int,
        action: str,
        *,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"依赖关系 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于依赖解析可执行范围"
        if actor is not None and not actor.can_write:
            return None, "观察者只读共享关系，不能对依赖关系执行维护动作"
        scope_error = validate_project_scope(
            upstream=entry.get("上游作业"),
            downstream=entry.get("下游作业"),
            project=entry.get("所属项目"),
            actor=actor,
        )
        if scope_error:
            return None, scope_error

        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = _text(entry.get("status"))
        if current not in ACTION_SOURCES[action]:
            return None, f"依赖关系当前为「{current}」，不能执行「{action}」"

        if target == ACTIVE_STATUS:
            # 同一对上下游的其它活动边会被本次生效取代，避免重复活动边。
            for row in store.rows(MODULE):
                if row is entry:
                    continue
                if _is_active(row) and _direction(row) == _direction(entry):
                    row["status"] = RELEASED_STATUS
                    row["active"] = False
                    row["pending"] = False
                    row["deactivated_at"] = _now()
            # 重新生效（草稿 -> 生效、解除 -> 生效）都会推进关系版本。
            if entry.get("status") != ACTIVE_STATUS or not entry.get("active"):
                entry["relation_version"] = _version(entry) + 1
            entry["active"] = True
            entry["deactivated_at"] = None
            entry["version_started_at"] = _now()
        elif target == RELEASED_STATUS:
            entry["active"] = False
            entry["deactivated_at"] = _now()
        elif entry.get("active"):
            # 离开已生效状态（如重新评审）时同步撤销活动边标记。
            entry["active"] = False

        entry["status"] = target
        entry["pending"] = target not in (STATUS_ORDER[-1], ACTIVE_STATUS)
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return _public(entry), f"依赖关系{action}操作已完成"

    def share_relation(
        self,
        relation_id: int,
        *,
        project: str | None = None,
        observer: str | None = None,
        maintainer: str | None = None,
        actor: Actor | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把关系共享给项目与观察者；观察者只读，不获得维护能力。"""

        entry = store.find(MODULE, relation_id)
        if entry is None:
            return None, f"依赖关系 {relation_id} 不存在或已归档"
        if actor is not None and not actor.can_write:
            return None, "观察者只读共享关系，不能调整共享授权"
        scope_error = validate_project_scope(
            upstream=entry.get("上游作业"),
            downstream=entry.get("下游作业"),
            project=project or entry.get("所属项目"),
            actor=actor,
        )
        if scope_error:
            return None, scope_error

        rows = store.rows("dependency_assignment")
        assignment = None
        for row in rows:
            if int(row.get("依赖关系", 0)) == relation_id:
                assignment = row
                break
        if assignment is None:
            assignment = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                          "依赖关系": relation_id,
                          "关系编号": entry.get("关系编号"),
                          "active": True}
            rows.append(assignment)
        assignment["项目"] = _text(project) or entry.get("所属项目")
        assignment["观察者"] = _text(observer) or assignment.get("观察者")
        assignment["维护者"] = _text(maintainer) or assignment.get("维护者")
        assignment["access_level"] = "观察者只读"
        return assignment, "共享关系已更新，观察者保持只读"

    def assignments(self, *, actor: Actor | None = None) -> list[dict[str, Any]]:
        rows = store.rows("dependency_assignment")
        if actor is not None and actor.has_global_scope:
            return rows
        if actor is None:
            return rows
        if actor.is_observer:
            observers = _text(actor.operator)
            return [
                row for row in rows
                if row.get("active") and (
                    _text(row.get("观察者")) == observers
                    or _text(row.get("项目")) == actor.project_name
                )
            ]
        return [row for row in rows if _text(row.get("项目")) == actor.project_name]
