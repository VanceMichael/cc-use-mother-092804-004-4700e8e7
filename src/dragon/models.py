"""龙舟联赛后台的领域模型与枚举。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(str, Enum):
    """联赛参与角色，决定可见范围与操作权限。"""

    SECRETARIAT = "秘书处"
    REFEREE = "裁判"
    TIMER = "计时员"
    TEAM_STAFF = "队伍工作人员"


class Gender(str, Enum):
    MALE = "男"
    FEMALE = "女"


class Position(str, Enum):
    PADDLER = "划手"
    DRUMMER = "鼓手"
    STEERER = "舵手"


class Category(str, Enum):
    MEN_OPEN = "男子公开组"
    MIXED = "男女混合组"


class MembershipStatus(str, Enum):
    ACTIVE = "在队"
    REPLACED = "已被替补"
    REMOVED = "已离队"


class EntryStatus(str, Enum):
    ENTERED = "已报名"
    WITHDRAWN = "已退赛"


class SubReason(str, Enum):
    INJURY = "伤病"
    ILLNESS = "突发疾病"
    EMERGENCY = "个人紧急事务"
    OTHER = "其他（须秘书处认可）"


VALID_SUB_REASONS = frozenset(
    {SubReason.INJURY, SubReason.ILLNESS, SubReason.EMERGENCY, SubReason.OTHER}
)


class SubstitutionStatus(str, Enum):
    PENDING = "待秘书处审批"
    APPROVED = "已批准"
    REJECTED = "已驳回"


class RaceStatus(str, Enum):
    ARRANGED = "已编排"
    RESULT_RECEIVED = "已收到成绩"
    SETTLED = "已结算"
    LOCKED = "积分锁定"


class LedgerStatus(str, Enum):
    CONFIRMED = "已确认"
    LOCKED = "冻结未决"
    VOIDED = "已作废"


class AppealStatus(str, Enum):
    OPEN = "未决"
    UPHELD = "申诉成立"
    REJECTED = "申诉驳回"
    WITHDRAWN = "撤回"


@dataclass
class User:
    uid: str
    name: str
    role: Role
    team_id: Optional[str] = None


@dataclass
class Person:
    pid: str
    name: str
    gender: Gender


@dataclass
class Team:
    tid: str
    name: str
    staff_user_ids: list[str] = field(default_factory=list)


@dataclass
class Membership:
    """队员与队伍的成员关系；替补不删除原关系，只改变状态。"""

    mid: str
    team_id: str
    person_id: str
    position: Position
    status: MembershipStatus = MembershipStatus.ACTIVE
    eligible: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[str] = None


@dataclass
class Stage:
    sid: str
    name: str
    seq: int


@dataclass
class CategoryRule:
    category: Category
    paddler_count: int
    min_female_paddlers: int


@dataclass
class Entry:
    """队伍在某分站某组别的报名。"""

    eid: str
    stage_id: str
    team_id: str
    category: Category
    status: EntryStatus = EntryStatus.ENTERED
    created_by: Optional[str] = None


@dataclass
class CheckInRow:
    membership_id: str
    person_id: str
    person_name: str
    gender: Gender
    position: Position


@dataclass
class CheckIn:
    """检录后冻结的上场名单快照（含替补后的新版本）。"""

    cid: str
    entry_id: str
    version: int
    rows: list[CheckInRow]
    paddler_count: int
    female_paddlers: int
    compliant: bool
    violations: list[str]
    frozen_by: str
    frozen_at: str


@dataclass
class Substitution:
    sid: str
    entry_id: str
    team_id: str
    original_membership_id: str
    original_person_id: str
    replacement_person_id: str
    position: Position
    reason: SubReason
    reason_detail: str
    status: SubstitutionStatus = SubstitutionStatus.PENDING
    requested_by: Optional[str] = None
    requested_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    # 秘书处必须明确积分与晋级是否受影响
    points_affected: Optional[bool] = None
    qualification_affected: Optional[bool] = None
    impact_note: Optional[str] = None


@dataclass
class Boat:
    bid: str
    label: str


@dataclass
class LaneAssignment:
    laid: str
    race_id: str
    team_id: str
    lane: int
    boat_id: str
    version: int
    reason: str
    changed_by: str
    changed_at: str
    points_affected: bool
    qualification_affected: bool
    impact_note: str


@dataclass
class Race:
    rid: str
    stage_id: str
    category: Category
    round_name: str
    status: RaceStatus = RaceStatus.ARRANGED


@dataclass
class ResultLine:
    team_id: str
    finish_ms: int
    place: int


@dataclass
class ResultSubmission:
    """一场成绩的一次接收；同内容重复接收返回首条，异内容构成争议版本。"""

    rsid: str
    race_id: str
    lines: tuple[ResultLine, ...]
    content_hash: str
    submitted_by: str
    submitted_at: str
    note: str = ""


@dataclass
class LedgerEntry:
    """积分台账：一队在一场的一条积分，随成绩版本递增。"""

    lid: str
    race_id: str
    stage_id: str
    category: Category
    team_id: str
    place: int
    points: int
    status: LedgerStatus
    result_version: int
    created_at: str
    note: str = ""


@dataclass
class Appeal:
    aid: str
    race_id: str
    team_id: Optional[str]
    reason: str
    status: AppealStatus = AppealStatus.OPEN
    raised_by: Optional[str] = None
    raised_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None
    selected_rsid: Optional[str] = None
    candidate_rsids: list[str] = field(default_factory=list)
    points_changed: bool = False
    qualification_affected: bool = False


@dataclass
class AuditEvent:
    """秘书处留痕：换人、赛道调整、成绩更正对积分/晋级的影响说明。"""

    eid: str
    at: str
    actor: str
    action: str
    target: str
    detail: str
    points_affected: bool
    qualification_affected: bool
