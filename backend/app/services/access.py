"""维护权限判定：项目归属、角色以及观察者的只读共享关系。

权限口径统一收在这里，依赖解析与执行模块的路由/服务只调用这里的结果，
避免不同入口对「谁能改」给出不一致的答案。

调用方通过请求头声明身份（均可缺省）：

- ``X-Operator``：操作人
- ``X-Role``：管理员 / 维护者 / 观察者（兼容 admin、maintainer、viewer 等写法）
- ``X-Project``：操作人所属项目

缺省身份是平台级维护者，保持本地起服务后的可操作性；一旦声明了项目，
维护动作就被限定在该项目内。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header

ADMIN_ROLES = {"管理员", "平台管理员", "超级管理员", "admin", "administrator", "superadmin"}
MAINTAINER_ROLES = {
    "维护者",
    "项目管理员",
    "项目维护者",
    "维护",
    "编辑",
    "开发者",
    "owner",
    "maintainer",
    "editor",
    "member",
    "user",
}
OBSERVER_ROLES = {"观察者", "只读", "访客", "observer", "viewer", "readonly", "read-only", "guest"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _header_text(value: object) -> str:
    """请求头按 latin-1 解码，中文身份需要再按 UTF-8 还原一次。"""

    raw = str(value or "")
    if not raw:
        return ""
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return raw.strip()


def _normalise_role(role: object) -> str:
    return _text(role).lower()


@dataclass(frozen=True)
class Actor:
    """一次请求的身份：操作人、角色与所属项目。"""

    operator: str = "值班管理员"
    role: str = "维护者"
    project: str | None = None

    @property
    def normalised_role(self) -> str:
        return _normalise_role(self.role)

    @property
    def is_admin(self) -> bool:
        return self.normalised_role in ADMIN_ROLES

    @property
    def is_observer(self) -> bool:
        role = self.normalised_role
        if role in ADMIN_ROLES or role in MAINTAINER_ROLES:
            return False
        # 未知角色按只读处理，宁可拒绝也不放开维护权限。
        return True

    @property
    def can_write(self) -> bool:
        return not self.is_observer

    @property
    def has_global_scope(self) -> bool:
        return self.is_admin or not _text(self.project)

    @property
    def project_name(self) -> str | None:
        name = _text(self.project)
        return name or None


def current_actor(
    x_operator: str | None = Header(default=None, alias="X-Operator"),
    x_role: str | None = Header(default=None, alias="X-Role"),
    x_project: str | None = Header(default=None, alias="X-Project"),
) -> Actor:
    """从请求头解析身份；全部缺省时返回平台级维护者。"""

    return Actor(
        operator=_header_text(x_operator) or "值班管理员",
        role=_header_text(x_role) or "维护者",
        project=_header_text(x_project) or None,
    )


def resolve_job_project(job: object) -> str | None:
    """按作业编号或作业名称查询作业档案中的所属项目。"""

    name = _text(job)
    if not name:
        return None
    for row in _etljob_rows():
        if name in {_text(row.get("作业编号")), _text(row.get("作业名称"))}:
            project = _text(row.get("所属项目"))
            if project:
                return project
    return None


def _etljob_rows() -> list[dict]:
    # 延迟导入，避免 access -> store -> seed 的初始化顺序问题。
    from app.store import store

    return store.rows("etljob")


def validate_project_scope(
    *,
    upstream: object,
    downstream: object,
    upstream_project: object = None,
    downstream_project: object = None,
    project: object = None,
    actor: Actor | None = None,
) -> str | None:
    """校验依赖关系的项目归属；返回错误说明，通过时返回 ``None``。

    规则：

    - 上游作业与下游作业分属不同项目时禁止登记/改动（跨项目改动被拒绝）；
    - 关系所属项目必须同时与上下游作业项目一致；
    - 声明了项目的维护者只能维护本项目的关系，观察者不参与判定（写入口单独拦截）。
    """

    up = _text(upstream_project) or resolve_job_project(upstream)
    down = _text(downstream_project) or resolve_job_project(downstream)
    rel = _text(project)

    if up and down and up != down:
        return f"上游作业与下游作业分属不同项目（{up} / {down}），禁止跨项目登记或改动依赖关系"
    if rel and up and rel != up:
        return f"依赖关系所属项目（{rel}）与上游作业项目（{up}）不一致，禁止跨项目改动"
    if rel and down and rel != down:
        return f"依赖关系所属项目（{rel}）与下游作业项目（{down}）不一致，禁止跨项目改动"

    if actor is not None and actor.project_name and not actor.has_global_scope:
        owned = rel or up or down
        if owned and owned != actor.project_name:
            return f"仅可维护所属项目（{actor.project_name}）的依赖关系，{owned} 不在维护范围内"
        if up and up != actor.project_name:
            return f"上游作业属于项目 {up}，不在当前项目（{actor.project_name}）维护范围内"
        if down and down != actor.project_name:
            return f"下游作业属于项目 {down}，不在当前项目（{actor.project_name}）维护范围内"
    return None
