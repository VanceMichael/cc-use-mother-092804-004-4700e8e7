"""龙舟联赛赛事后台的领域模型、常量与异常。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class LeagueError(ValueError):
    """业务规则校验失败。"""


class PermissionDenied(LeagueError):
    """当前角色无权执行该操作。"""


class Gender(enum.Enum):
    MALE = "男"
    FEMALE = "女"


class GroupCode(enum.Enum):
    MEN_OPEN = "男子公开组"
    MIXED = "男女混合组"


@dataclass(frozen=True)
class GroupRule:
    """组别规则：划手人数、女子下限与是否仅限男子。"""

    code: GroupCode
    paddler_count: int
    min_female: int
    male_only: bool = False


GROUP_RULES: dict[GroupCode, GroupRule] = {
    GroupCode.MEN_OPEN: GroupRule(GroupCode.MEN_OPEN, paddler_count=20, min_female=0, male_only=True),
    GroupCode.MIXED: GroupRule(GroupCode.MIXED, paddler_count=20, min_female=8),
}


class Role(enum.Enum):
    ADMIN = "秘书处"
    REFEREE = "裁判"
    TIMER = "计时员"
    TEAM_STAFF = "队伍工作人员"


@dataclass(frozen=True)
class Actor:
    """操作者：角色决定权限，队伍工作人员须关联队伍。"""

    actor_id: str
    role: Role
    team_id: str | None = None


class EntryStatus(enum.Enum):
    ENTERED = "已报名"
    CHECKED_IN = "已检录冻结"


class RaceStatus(enum.Enum):
    SCHEDULED = "已排期"
    FINISHED = "已出成绩"
    CONFLICT = "成绩冲突锁定"


class AppealStatus(enum.Enum):
    PENDING = "待处理"
    UPHELD = "成立"
    REJECTED = "驳回"


# 检录冻结后仅接受这些换人理由。
VALID_SUBSTITUTION_REASONS = frozenset({"伤病", "突发疾病", "不可抗力"})

# 名次对应的分站积分，表外名次一律记保底分。
POINTS_BY_RANK = {1: 10, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3, 8: 2}
FALLBACK_POINTS = 1

# 需要秘书处出具影响说明的事件类型。
IMPACT_KINDS = frozenset({"换人", "赛道调整", "成绩更正"})


@dataclass(frozen=True)
class Team:
    team_id: str
    name: str


@dataclass
class Member:
    """队员：资格可被秘书处调整，队伍关系不因被换下而解除。"""

    member_id: str
    team_id: str
    name: str
    gender: Gender
    eligible: bool = True
    eligibility_note: str = ""


@dataclass
class Entry:
    """分站报名：检录后名单冻结。"""

    entry_id: str
    team_id: str
    station: int
    group: GroupCode
    paddler_ids: tuple[str, ...]
    status: EntryStatus = EntryStatus.ENTERED


@dataclass(frozen=True)
class Substitution:
    """换人记录：保留原成员与替补的关系链。"""

    substitution_id: str
    entry_id: str
    out_member_id: str
    in_member_id: str
    reason: str
    requested_by: Role
    impact_event_id: str


@dataclass(frozen=True)
class Boat:
    boat_id: str
    label: str
    seats: int = 22


@dataclass(frozen=True)
class ResultRecord:
    """一场比赛的生效成绩及其内容摘要。"""

    receipt_id: str
    race_id: str
    times: dict[str, float]
    content_hash: str
    submitted_by: str
    submitter_role: Role


@dataclass(frozen=True)
class ConflictRecord:
    """与生效成绩不一致的提交，登记后锁定该场积分。"""

    receipt_id: str
    times: dict[str, float]
    content_hash: str
    submitted_by: str
    submitter_role: Role


@dataclass
class Race:
    race_id: str
    station: int
    group: GroupCode
    round_name: str
    lanes: dict[int, str]
    boats: dict[str, str]
    status: RaceStatus = RaceStatus.SCHEDULED
    result: ResultRecord | None = None
    conflicts: list[ConflictRecord] = field(default_factory=list)
    points_locked: bool = False


@dataclass(frozen=True)
class RacePoints:
    """积分版本中单场次的明细，供晋级解释追溯。"""

    race_id: str
    round_name: str
    station: int
    ranks: dict[str, int]
    points: dict[str, int]
    times: dict[str, float]


@dataclass(frozen=True)
class PointsVersion:
    """一次结算产生的积分版本，版本号只增不减。"""

    version: int
    station: int
    group: GroupCode
    points: dict[str, int]
    race_points: tuple[RacePoints, ...]
    locked_races: tuple[str, ...]


@dataclass
class Appeal:
    appeal_id: str
    race_id: str
    entry_id: str
    reason: str
    filed_by: str
    filer_role: Role
    status: AppealStatus = AppealStatus.PENDING
    decided_by: str | None = None


@dataclass(frozen=True)
class ImpactStatement:
    """秘书处对积分和晋级是否受影响的说明。"""

    points_affected: bool
    qualification_affected: bool
    explanation: str
    filed_by: str


@dataclass
class ImpactEvent:
    """换人、赛道调整或成绩更正事件，须附秘书处影响说明。"""

    event_id: str
    kind: str
    summary: str
    race_id: str | None = None
    entry_id: str | None = None
    team_id: str | None = None
    statement: ImpactStatement | None = None
