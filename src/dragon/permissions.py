"""角色权限与可见范围策略。"""

from __future__ import annotations

from .errors import PermissionDeniedError
from .models import AppealStatus, LedgerStatus, Role


# 各角色允许调用的操作。
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.SECRETARIAT: frozenset(
        {
            "team.register",
            "person.register",
            "membership.verify",
            "entry.create",
            "entry.withdraw",
            "checkin.freeze",
            "substitution.review",
            "race.arrange",
            "lane.adjust",
            "result.correct",
            "appeal.resolve",
            "audit.read",
            "qualification.read",
        }
    ),
    Role.REFEREE: frozenset(
        {
            "race.arrange",
            "result.submit",
            "result.confirm",
            "appeal.raise",
            "checkin.read",
            "result.read",
            "qualification.read",
        }
    ),
    Role.TIMER: frozenset(
        {
            "result.submit",
            "checkin.read",
            "result.read",
        }
    ),
    Role.TEAM_STAFF: frozenset(
        {
            "entry.create",
            "substitution.request",
            "appeal.raise",
            "checkin.read.own",
            "result.read.own",
            "qualification.read.own",
        }
    ),
}

# 各角色可查询的数据范围。
SCOPE_ALL = "全部队伍、全部组别"
SCOPE_OFFICIAL = "场次编排、已确认成绩与公开积分"
SCOPE_OWN_TEAM = "本队检录、本队成绩与本队晋级解释"
SCOPE_NONE = "无权查看"

VISIBLE_SCOPE: dict[Role, str] = {
    Role.SECRETARIAT: SCOPE_ALL,
    Role.REFEREE: SCOPE_OFFICIAL,
    Role.TIMER: SCOPE_OFFICIAL,
    Role.TEAM_STAFF: SCOPE_OWN_TEAM,
}


def require(role: Role, action: str) -> None:
    if action not in PERMISSIONS.get(role, frozenset()):
        raise PermissionDeniedError(f"{role.value}无权执行：{action}")


def ensure_team_scope(role: Role, user_team_id: str | None, target_team_id: str) -> None:
    """队伍工作人员只能看/操作本队。"""

    if role == Role.TEAM_STAFF and user_team_id != target_team_id:
        raise PermissionDeniedError("队伍工作人员只能访问本队数据")


def ledger_visible_for(role: Role, status: LedgerStatus) -> bool:
    """非秘书处角色只能看到已确认积分，锁定中的积分不向队伍展示具体值。"""

    if role == Role.SECRETARIAT:
        return True
    return status == LedgerStatus.CONFIRMED


def appeal_visible_for(role: Role, team_id: str | None, user_team_id: str | None) -> bool:
    if role == Role.SECRETARIAT or role == Role.REFEREE:
        return True
    if role == Role.TEAM_STAFF:
        return team_id == user_team_id
    return False


def open_appeal_exists(status: AppealStatus) -> bool:
    return status == AppealStatus.OPEN
