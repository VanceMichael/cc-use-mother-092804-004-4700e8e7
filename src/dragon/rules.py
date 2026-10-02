"""组别规则、积分表与资格判定。"""

from __future__ import annotations

from .models import (
    Category,
    CategoryRule,
    CheckInRow,
    Membership,
    MembershipStatus,
    Position,
)

# 每队二十名划手；混合组女子划手不少于八人。
MIXED_PADDLER_COUNT = 20
MIXED_MIN_FEMALE_PADDLERS = 8
MEN_OPEN_PADDLER_COUNT = 20

CATEGORY_RULES: dict[Category, CategoryRule] = {
    Category.MEN_OPEN: CategoryRule(Category.MEN_OPEN, MEN_OPEN_PADDLER_COUNT, 0),
    Category.MIXED: CategoryRule(
        Category.MIXED, MIXED_PADDLER_COUNT, MIXED_MIN_FEMALE_PADDLERS
    ),
}

# 分站名次积分表：名次 -> 积分。名次越靠前积分越高。
PLACE_POINTS: dict[int, int] = {
    1: 100,
    2: 85,
    3: 70,
    4: 58,
    5: 48,
    6: 40,
    7: 32,
    8: 26,
}

# 前四个分站决定年度总决赛分组。
FINAL_QUALIFYING_STAGES = 4


def points_for_place(place: int) -> int:
    """取名次积分；表外名次给保底积分。"""

    if place in PLACE_POINTS:
        return PLACE_POINTS[place]
    if place < 1:
        raise ValueError("名次必须为正整数")
    return 20


def check_composition(rows: list[CheckInRow], category: Category) -> list[str]:
    """校验检录名单是否满足划手人数与混合组女子比例。"""

    rule = CATEGORY_RULES[category]
    violations: list[str] = []
    paddlers = [r for r in rows if r.position == Position.PADDLER]
    female = [r for r in paddlers if r.gender.value == "女"]
    if len(rows) == 0:
        violations.append("检录名单为空")
    if len(paddlers) != rule.paddler_count:
        violations.append(
            f"{category.value}划手人数应为{rule.paddler_count}人，实际{len(paddlers)}人"
        )
    if len(female) < rule.min_female_paddlers:
        violations.append(
            f"{category.value}二十名划手中女子不少于{rule.min_female_paddlers}人，"
            f"实际{len(female)}人"
        )
    # 混合组中，鼓手与舵手的性别不计入“八名女划手”的口径。
    return violations


def active_membership(memberships: list[Membership], person_id: str) -> Membership | None:
    for m in memberships:
        if m.person_id == person_id and m.status == MembershipStatus.ACTIVE:
            return m
    return None
