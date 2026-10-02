"""晋级查询：解释每支队伍的积分来源、组别资格、换人记录与未决申诉。"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import permissions as perm
from .models import (
    Appeal,
    Category,
    EntryStatus,
    LedgerEntry,
    LedgerStatus,
    Role,
    Substitution,
    SubstitutionStatus,
)
from .rules import FINAL_QUALIFYING_STAGES
from .service import LeagueService


@dataclass
class PointSource:
    """一条可追溯的积分来源。"""

    stage_name: str
    race_id: str
    round_name: str
    place: int
    points: int
    status: LedgerStatus
    result_version: int
    note: str


@dataclass
class QualificationView:
    team_id: str
    team_name: str
    category: Category
    entries: list[dict] = field(default_factory=list)
    point_sources: list[PointSource] = field(default_factory=list)
    locked_points: int = 0
    confirmed_points: int = 0
    pending_points: int = 0
    substitutions: list[Substitution] = field(default_factory=list)
    open_appeals: list[Appeal] = field(default_factory=list)
    compliant: bool = True
    qualification_notes: list[str] = field(default_factory=list)


@dataclass
class FinalGroup:
    category: Category
    group_name: str
    team_id: str
    team_name: str
    confirmed_points: int
    locked_points: int
    rank: int
    explanation: QualificationView


# 总决赛分组：积分前若干名进入冠军组（示例口径取前 4 名），其余进入挑战组。
CHAMPION_GROUP_SIZE = 4


class QualificationQuery:
    def __init__(self, service: LeagueService) -> None:
        self.service = service
        self.store = service.store

    def team_view(
        self, actor_id: str, team_id: str, category: Category
    ) -> QualificationView:
        actor = self.service._user(actor_id)
        if actor.role == Role.TEAM_STAFF:
            perm.require(actor.role, "qualification.read.own")
        else:
            perm.require(actor.role, "qualification.read")
        perm.ensure_team_scope(actor.role, actor.team_id, team_id)

        team = self.service._team(team_id)
        view = QualificationView(
            team_id=team_id, team_name=team.name, category=category
        )

        # 报名与检录资格
        for entry in sorted(
            (
                e
                for e in self.store.entries.values()
                if e.team_id == team_id
                and e.category == category
                and e.status == EntryStatus.ENTERED
            ),
            key=lambda e: self.store.stages[e.stage_id].seq,
        ):
            stage = self.store.stages[entry.stage_id]
            checkin = self.service.latest_checkin(entry.eid)
            row = {
                "stage": stage.name,
                "entry_id": entry.eid,
                "checked_in": checkin is not None,
            }
            if checkin is not None:
                row.update(
                    {
                        "version": checkin.version,
                        "paddlers": checkin.paddler_count,
                        "female_paddlers": checkin.female_paddlers,
                        "compliant": checkin.compliant,
                        "violations": checkin.violations,
                    }
                )
                if not checkin.compliant:
                    view.compliant = False
                    view.qualification_notes.append(
                        f"{stage.name}检录名单不合规：{'；'.join(checkin.violations)}"
                    )
            else:
                view.qualification_notes.append(f"{stage.name}尚未完成检录冻结")
            view.entries.append(row)

        # 积分来源（只计前四个分站）
        qualifying_ids = {
            s.sid for s in self.store.stages.values() if s.seq <= FINAL_QUALIFYING_STAGES
        }
        for race_id, ledger in self.store.ledger.items():
            race = self.store.races[race_id]
            if race.category != category or race.stage_id not in qualifying_ids:
                continue
            for e in ledger:
                if e.team_id != team_id:
                    continue
                if e.status == LedgerStatus.VOIDED:
                    continue
                if not perm.ledger_visible_for(actor.role, e.status):
                    # 队伍侧不展示冻结中的具体分值，只提示存在未决。
                    view.pending_points += e.points
                    view.point_sources.append(
                        PointSource(
                            stage_name=self.store.stages[race.stage_id].name,
                            race_id=race_id,
                            round_name=race.round_name,
                            place=e.place,
                            points=0,
                            status=e.status,
                            result_version=e.result_version,
                            note="积分冻结中，待争议裁决后公布",
                        )
                    )
                    continue
                view.point_sources.append(
                    PointSource(
                        stage_name=self.store.stages[race.stage_id].name,
                        race_id=race_id,
                        round_name=race.round_name,
                        place=e.place,
                        points=e.points,
                        status=e.status,
                        result_version=e.result_version,
                        note=e.note,
                    )
                )
                if e.status == LedgerStatus.LOCKED:
                    view.locked_points += e.points
                else:
                    view.confirmed_points += e.points

        view.point_sources.sort(key=lambda p: (p.race_id, p.result_version))
        view.substitutions = self.service.team_substitutions(team_id)
        if any(s.status == SubstitutionStatus.PENDING for s in view.substitutions):
            view.qualification_notes.append("存在待秘书处审批的替补申请")
        view.open_appeals = self.service.open_appeals_for_team(team_id)
        if view.open_appeals:
            view.qualification_notes.append(
                f"存在{len(view.open_appeals)}起未决申诉，相关积分暂不计入确定排名"
            )
        return view

    def final_groups(
        self, actor_id: str, category: Category
    ) -> dict[str, list[FinalGroup]]:
        """按已确认积分排名生成年度总决赛分组，并附带逐队解释。"""

        actor = self.service._user(actor_id)
        perm.require(actor.role, "qualification.read")

        team_views: list[QualificationView] = [
            self.team_view(actor.uid, tid, category) for tid in self.store.teams
        ]
        team_views = [
            v
            for v in team_views
            if v.entries and any(e["checked_in"] for e in v.entries)
        ]
        team_views.sort(key=lambda v: v.confirmed_points, reverse=True)

        groups: dict[str, list[FinalGroup]] = {"冠军组": [], "挑战组": []}
        for rank, v in enumerate(team_views, start=1):
            group_name = "冠军组" if rank <= CHAMPION_GROUP_SIZE else "挑战组"
            groups[group_name].append(
                FinalGroup(
                    category=category,
                    group_name=group_name,
                    team_id=v.team_id,
                    team_name=v.team_name,
                    confirmed_points=v.confirmed_points,
                    locked_points=v.locked_points,
                    rank=rank,
                    explanation=v,
                )
            )
        return groups

    def explain(self, actor_id: str, team_id: str, category: Category) -> str:
        """生成面向人的文字解释。"""

        v = self.team_view(actor_id, team_id, category)
        lines = [
            f"【{v.team_name} · {v.category.value} 晋级解释】",
            f"已确认积分：{v.confirmed_points}；冻结积分：{v.locked_points}；"
            f"待公布：{v.pending_points}",
            "积分来源：",
        ]
        if not v.point_sources:
            lines.append("  （暂无积分记录）")
        for p in v.point_sources:
            shown = p.points if p.status == LedgerStatus.CONFIRMED else "冻结"
            lines.append(
                f"  - {p.stage_name} {p.round_name}（{p.race_id}）：第{p.place}名，"
                f"{shown}分，成绩版本v{p.result_version}，{p.status.value}｜{p.note}"
            )

        lines.append("组别资格：")
        for e in v.entries:
            if e["checked_in"]:
                lines.append(
                    f"  - {e['stage']}：已检录 v{e['version']}，划手{e['paddlers']}人"
                    + (
                        f"，女划手{e['female_paddlers']}人，合规"
                        if v.category == Category.MIXED
                        else "，合规"
                    )
                    if e["compliant"]
                    else f"  - {e['stage']}：不合规（{'；'.join(e['violations'])}）"
                )
            else:
                lines.append(f"  - {e['stage']}：未检录")

        lines.append("换人记录：")
        if not v.substitutions:
            lines.append("  （无）")
        for s in v.substitutions:
            lines.append(
                f"  - {s.sid}：{s.original_person_id}→{s.replacement_person_id}"
                f"（{s.reason.value}），{s.status.value}"
                + (
                    f"｜积分影响：{'是' if s.points_affected else '否'}，"
                    f"晋级影响：{'是' if s.qualification_affected else '否'}，{s.impact_note}"
                    if s.status == SubstitutionStatus.APPROVED
                    else ""
                )
            )

        lines.append("未决申诉：")
        if not v.open_appeals:
            lines.append("  （无）")
        for a in v.open_appeals:
            lines.append(f"  - {a.aid}（场次{a.race_id}）：{a.reason}")
        if v.qualification_notes:
            lines.append("备注：")
            lines.extend(f"  - {n}" for n in v.qualification_notes)
        return "\n".join(lines)
