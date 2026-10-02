"""龙舟联赛赛事后台：注册、检录、成绩、积分与晋级解释。

所有业务规则在此强制生效：检录冻结、有效理由换人、成绩幂等接收、
积分版本只增不减、提交成绩的人不能独自确认争议、秘书处影响说明。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.league_model import (
    GROUP_RULES,
    IMPACT_KINDS,
    POINTS_BY_RANK,
    FALLBACK_POINTS,
    VALID_SUBSTITUTION_REASONS,
    Actor,
    Appeal,
    AppealStatus,
    Boat,
    ConflictRecord,
    Entry,
    EntryStatus,
    Gender,
    GroupCode,
    ImpactEvent,
    ImpactStatement,
    LeagueError,
    Member,
    PermissionDenied,
    PointsVersion,
    Race,
    RacePoints,
    RaceStatus,
    ResultRecord,
    Role,
    Substitution,
    Team,
)


def _hash_payload(payload: dict) -> str:
    """生成与键顺序无关的内容摘要，用于识别重复提交。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Receipt:
    """成绩接收回执：重复提交同一内容时返回原回执与生效成绩。"""

    receipt_id: str
    race_id: str
    accepted: bool
    replayed: bool
    conflict: bool
    points_locked: bool
    times: dict[str, float]


class LeagueBackend:
    """赛事后台服务，管理一个赛季全部分站的数据。"""

    def __init__(self, station_count: int = 4) -> None:
        if station_count < 1:
            raise LeagueError("分站数量无效")
        self.station_count = station_count
        self._teams: dict[str, Team] = {}
        self._members: dict[str, Member] = {}
        self._entries: dict[str, Entry] = {}
        self._boats: dict[str, Boat] = {}
        self._races: dict[str, Race] = {}
        self._substitutions: dict[str, Substitution] = {}
        self._appeals: dict[str, Appeal] = {}
        self._impact_events: dict[str, ImpactEvent] = {}
        self._versions: list[PointsVersion] = []
        self._effective: dict[tuple[int, GroupCode], PointsVersion] = {}
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]}"

    def _team(self, team_id: str) -> Team:
        try:
            return self._teams[team_id]
        except KeyError:
            raise LeagueError("队伍不存在") from None

    def _member(self, member_id: str) -> Member:
        try:
            return self._members[member_id]
        except KeyError:
            raise LeagueError("队员不存在") from None

    def _entry(self, entry_id: str) -> Entry:
        try:
            return self._entries[entry_id]
        except KeyError:
            raise LeagueError("报名不存在") from None

    def _boat(self, boat_id: str) -> Boat:
        try:
            return self._boats[boat_id]
        except KeyError:
            raise LeagueError("船只不存在") from None

    def _race(self, race_id: str) -> Race:
        try:
            return self._races[race_id]
        except KeyError:
            raise LeagueError("场次不存在") from None

    def _appeal(self, appeal_id: str) -> Appeal:
        try:
            return self._appeals[appeal_id]
        except KeyError:
            raise LeagueError("申诉不存在") from None

    def _check_station(self, station: int) -> None:
        if not 1 <= station <= self.station_count:
            raise LeagueError("分站编号无效")

    @staticmethod
    def _require_role(by: Actor, roles: set[Role]) -> None:
        if by.role not in roles:
            raise PermissionDenied("当前角色无权执行该操作")

    @staticmethod
    def _require_team_or_admin(by: Actor, team_id: str) -> None:
        if by.role is Role.ADMIN:
            return
        if by.role is Role.TEAM_STAFF and by.team_id == team_id:
            return
        raise PermissionDenied("当前角色无权执行该操作")

    # ------------------------------------------------------------------
    # 队伍注册与队员资格
    # ------------------------------------------------------------------

    def register_team(self, name: str) -> str:
        if not name.strip():
            raise LeagueError("队伍名称不能为空")
        team_id = self._next_id("T")
        self._teams[team_id] = Team(team_id=team_id, name=name.strip())
        return team_id

    def register_member(self, team_id: str, name: str, gender: Gender) -> str:
        self._team(team_id)
        if not name.strip():
            raise LeagueError("队员姓名不能为空")
        member_id = self._next_id("M")
        self._members[member_id] = Member(member_id, team_id, name.strip(), gender)
        return member_id

    def set_member_eligibility(self, member_id: str, eligible: bool, note: str, by: Actor) -> None:
        self._require_role(by, {Role.ADMIN})
        member = self._member(member_id)
        member.eligible = bool(eligible)
        member.eligibility_note = note.strip()

    # ------------------------------------------------------------------
    # 分站报名与检录冻结
    # ------------------------------------------------------------------

    def enter_station(self, team_id: str, station: int, group: GroupCode, paddler_ids: list[str]) -> str:
        self._team(team_id)
        self._check_station(station)
        if any(
            e.team_id == team_id and e.station == station and e.group is group
            for e in self._entries.values()
        ):
            raise LeagueError("该队在此分站此组别已报名")
        self._check_roster(team_id, group, list(paddler_ids))
        entry_id = self._next_id("E")
        self._entries[entry_id] = Entry(entry_id, team_id, station, group, tuple(paddler_ids))
        return entry_id

    def _check_roster(self, team_id: str, group: GroupCode, paddler_ids: list[str]) -> None:
        rule = GROUP_RULES[group]
        if len(paddler_ids) != rule.paddler_count:
            raise LeagueError(f"{group.value}须为{rule.paddler_count}名划手")
        if len(set(paddler_ids)) != len(paddler_ids):
            raise LeagueError("名单存在重复队员")
        members = [self._member(m) for m in paddler_ids]
        for member in members:
            if member.team_id != team_id:
                raise LeagueError("划手不属于该队伍")
            if not member.eligible:
                raise LeagueError(f"队员{member.name}资格无效")
        female = sum(1 for m in members if m.gender is Gender.FEMALE)
        if rule.male_only and female:
            raise LeagueError("男子公开组不允许女子队员")
        if female < rule.min_female:
            raise LeagueError(f"{group.value}女子队员不得少于{rule.min_female}人")

    def check_in(self, entry_id: str, by: Actor) -> None:
        """检录后名单冻结，之后只能按有效理由换人。"""
        self._require_role(by, {Role.ADMIN, Role.REFEREE})
        entry = self._entry(entry_id)
        if entry.status is not EntryStatus.ENTERED:
            raise LeagueError("仅已报名名单可检录")
        entry.status = EntryStatus.CHECKED_IN

    # ------------------------------------------------------------------
    # 换人：保留原成员关系，须秘书处说明影响
    # ------------------------------------------------------------------

    def substitute(self, entry_id: str, out_member_id: str, in_member_id: str, reason: str, by: Actor) -> str:
        entry = self._entry(entry_id)
        self._require_team_or_admin(by, entry.team_id)
        if entry.status is not EntryStatus.CHECKED_IN:
            raise LeagueError("检录冻结后才能办理换人")
        if reason not in VALID_SUBSTITUTION_REASONS:
            raise LeagueError("换人理由无效")
        paddlers = list(entry.paddler_ids)
        if out_member_id not in paddlers:
            raise LeagueError("被替换队员不在名单中")
        incoming = self._member(in_member_id)
        if incoming.team_id != entry.team_id:
            raise LeagueError("替补须为本队队员")
        if not incoming.eligible:
            raise LeagueError("替补队员资格无效")
        if in_member_id in paddlers:
            raise LeagueError("替补队员已在名单中")
        paddlers[paddlers.index(out_member_id)] = in_member_id
        self._check_roster(entry.team_id, entry.group, paddlers)
        entry.paddler_ids = tuple(paddlers)
        substitution_id = self._next_id("S")
        event_id = self._new_impact_event(
            "换人",
            f"报名{entry_id}：{out_member_id}换为{in_member_id}（{reason}）",
            entry_id=entry_id,
            team_id=entry.team_id,
        )
        self._substitutions[substitution_id] = Substitution(
            substitution_id, entry_id, out_member_id, in_member_id, reason, by.role, event_id
        )
        return substitution_id

    # ------------------------------------------------------------------
    # 赛道与船只
    # ------------------------------------------------------------------

    def register_boat(self, label: str, seats: int = 22) -> str:
        if not label.strip():
            raise LeagueError("船只名称不能为空")
        if seats < 1:
            raise LeagueError("船只座位数无效")
        boat_id = self._next_id("B")
        self._boats[boat_id] = Boat(boat_id, label.strip(), seats)
        return boat_id

    def schedule_race(
        self,
        station: int,
        group: GroupCode,
        round_name: str,
        lanes: dict[int, str],
        boats: dict[str, str],
        by: Actor,
    ) -> str:
        self._require_role(by, {Role.ADMIN})
        self._check_station(station)
        if not lanes:
            raise LeagueError("赛道安排不能为空")
        entry_ids = list(lanes.values())
        if len(set(entry_ids)) != len(entry_ids):
            raise LeagueError("同一报名不能占用多条赛道")
        if any(lane < 1 for lane in lanes):
            raise LeagueError("赛道编号无效")
        for entry_id in entry_ids:
            entry = self._entry(entry_id)
            if entry.station != station or entry.group is not group:
                raise LeagueError("报名与场次分站或组别不符")
            if entry.status is not EntryStatus.CHECKED_IN:
                raise LeagueError("未检录冻结的报名不能编排")
        for entry_id, boat_id in boats.items():
            if entry_id not in entry_ids:
                raise LeagueError("船只分配了未编排的报名")
            self._boat(boat_id)
        race_id = self._next_id("R")
        self._races[race_id] = Race(race_id, station, group, round_name.strip(), dict(lanes), dict(boats))
        return race_id

    def adjust_lane(self, race_id: str, entry_id: str, new_lane: int, by: Actor) -> str:
        """调整赛道并登记影响事件，秘书处须说明积分和晋级是否受影响。"""
        self._require_role(by, {Role.ADMIN})
        race = self._race(race_id)
        if race.result is not None:
            raise LeagueError("已出成绩的场次不能调整赛道")
        if entry_id not in race.lanes.values():
            raise LeagueError("报名不在该场次")
        if new_lane < 1:
            raise LeagueError("赛道编号无效")
        if new_lane in race.lanes:
            raise LeagueError("目标赛道已被占用")
        old_lane = next(lane for lane, eid in race.lanes.items() if eid == entry_id)
        del race.lanes[old_lane]
        race.lanes[new_lane] = entry_id
        return self._new_impact_event(
            "赛道调整", f"场次{race_id}：报名{entry_id}由{old_lane}道调至{new_lane}道", race_id=race_id
        )

    # ------------------------------------------------------------------
    # 成绩接收：幂等，冲突锁定该场积分
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_times(race: Race, times: dict[str, float]) -> dict[str, float]:
        if set(times) != set(race.lanes.values()):
            raise LeagueError("成绩须覆盖该场次全部报名")
        clean: dict[str, float] = {}
        for entry_id, value in times.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise LeagueError("成绩时间无效")
            clean[entry_id] = float(value)
        return clean

    def submit_result(self, race_id: str, times: dict[str, float], by: Actor) -> Receipt:
        """接收成绩：重复提交同一内容返回原回执，内容不一致锁定该场积分。"""
        self._require_role(by, {Role.TIMER, Role.REFEREE})
        race = self._race(race_id)
        clean = self._validate_times(race, times)
        content_hash = _hash_payload({"race_id": race_id, "times": clean})
        if race.result is None:
            receipt_id = self._next_id("RC")
            race.result = ResultRecord(receipt_id, race_id, clean, content_hash, by.actor_id, by.role)
            race.status = RaceStatus.FINISHED
            return Receipt(receipt_id, race_id, True, False, False, False, dict(clean))
        existing = race.result
        if content_hash == existing.content_hash:
            return Receipt(existing.receipt_id, race_id, True, True, False, race.points_locked, dict(existing.times))
        for conflict in race.conflicts:
            if conflict.content_hash == content_hash:
                return Receipt(conflict.receipt_id, race_id, False, True, True, True, dict(existing.times))
        receipt_id = self._next_id("RC")
        race.conflicts.append(ConflictRecord(receipt_id, clean, content_hash, by.actor_id, by.role))
        race.points_locked = True
        race.status = RaceStatus.CONFLICT
        return Receipt(receipt_id, race_id, False, False, True, True, dict(existing.times))

    def correct_result(self, race_id: str, times: dict[str, float], reason: str, by: Actor) -> Receipt:
        """更正成绩：解除锁定并登记影响事件，重新结算后产生新版本。"""
        self._require_role(by, {Role.REFEREE, Role.ADMIN})
        race = self._race(race_id)
        if race.result is None:
            raise LeagueError("尚未接收成绩，无需更正")
        if not reason.strip():
            raise LeagueError("更正须说明理由")
        clean = self._validate_times(race, times)
        content_hash = _hash_payload({"race_id": race_id, "times": clean})
        receipt_id = self._next_id("RC")
        race.result = ResultRecord(receipt_id, race_id, clean, content_hash, by.actor_id, by.role)
        race.conflicts.clear()
        race.points_locked = False
        race.status = RaceStatus.FINISHED
        self._new_impact_event("成绩更正", f"场次{race_id}成绩更正：{reason.strip()}", race_id=race_id)
        return Receipt(receipt_id, race_id, True, False, False, False, dict(clean))

    # ------------------------------------------------------------------
    # 积分结算与版本
    # ------------------------------------------------------------------

    def settle(self, station: int, group: GroupCode, by: Actor) -> PointsVersion:
        """结算一个分站一个组别：跳过锁定场次，内容未变时返回原版本。"""
        self._require_role(by, {Role.ADMIN})
        self._check_station(station)
        races = [r for r in self._races.values() if r.station == station and r.group is group]
        if not races:
            raise LeagueError("该分站组别没有场次")
        locked = tuple(sorted(r.race_id for r in races if r.points_locked))
        race_points: list[RacePoints] = []
        points: dict[str, int] = {}
        for race in sorted(races, key=lambda r: r.race_id):
            if race.result is None or race.points_locked:
                continue
            ranked = sorted(race.result.times.items(), key=lambda kv: (kv[1], kv[0]))
            ranks = {entry_id: rank for rank, (entry_id, _) in enumerate(ranked, start=1)}
            race_pts = {eid: POINTS_BY_RANK.get(rank, FALLBACK_POINTS) for eid, rank in ranks.items()}
            race_points.append(
                RacePoints(race.race_id, race.round_name, race.station, ranks, race_pts, dict(race.result.times))
            )
            for entry_id, pts in race_pts.items():
                points[entry_id] = points.get(entry_id, 0) + pts
        if not race_points and not locked:
            raise LeagueError("该分站组别没有可结算的成绩")
        key = (station, group)
        current = self._effective.get(key)
        if current and current.points == points and current.race_points == tuple(race_points) and current.locked_races == locked:
            return current
        version = PointsVersion(len(self._versions) + 1, station, group, points, tuple(race_points), locked)
        self._versions.append(version)
        self._effective[key] = version
        return version

    def points_history(self) -> list[PointsVersion]:
        """全部积分版本，版本号只增不减。"""
        return list(self._versions)

    # ------------------------------------------------------------------
    # 申诉：提交成绩的人不能独自确认争议
    # ------------------------------------------------------------------

    def file_appeal(self, race_id: str, entry_id: str, reason: str, by: Actor) -> str:
        race = self._race(race_id)
        if race.result is None:
            raise LeagueError("尚未出成绩，不能申诉")
        if entry_id not in race.lanes.values():
            raise LeagueError("报名不在该场次")
        if not reason.strip():
            raise LeagueError("申诉理由不能为空")
        if by.role is Role.TEAM_STAFF:
            self._require_team_or_admin(by, self._entry(entry_id).team_id)
        elif by.role not in (Role.REFEREE, Role.ADMIN):
            raise PermissionDenied("当前角色无权执行该操作")
        appeal_id = self._next_id("A")
        self._appeals[appeal_id] = Appeal(appeal_id, race_id, entry_id, reason.strip(), by.actor_id, by.role)
        return appeal_id

    def decide_appeal(self, appeal_id: str, uphold: bool, by: Actor) -> Appeal:
        self._require_role(by, {Role.REFEREE, Role.ADMIN})
        appeal = self._appeal(appeal_id)
        if appeal.status is not AppealStatus.PENDING:
            raise LeagueError("申诉已处理")
        race = self._race(appeal.race_id)
        if race.result is not None and race.result.submitted_by == by.actor_id:
            raise PermissionDenied("提交成绩的人不能独自确认争议")
        appeal.status = AppealStatus.UPHELD if uphold else AppealStatus.REJECTED
        appeal.decided_by = by.actor_id
        return appeal

    # ------------------------------------------------------------------
    # 影响说明：换人、赛道调整、成绩更正
    # ------------------------------------------------------------------

    def _new_impact_event(
        self,
        kind: str,
        summary: str,
        *,
        race_id: str | None = None,
        entry_id: str | None = None,
        team_id: str | None = None,
    ) -> str:
        if kind not in IMPACT_KINDS:
            raise LeagueError("影响事件类型无效")
        event_id = self._next_id("EV")
        self._impact_events[event_id] = ImpactEvent(event_id, kind, summary, race_id, entry_id, team_id)
        return event_id

    def file_impact_statement(
        self,
        event_id: str,
        points_affected: bool,
        qualification_affected: bool,
        explanation: str,
        by: Actor,
    ) -> ImpactEvent:
        """秘书处说明该事件是否影响积分和晋级。"""
        self._require_role(by, {Role.ADMIN})
        try:
            event = self._impact_events[event_id]
        except KeyError:
            raise LeagueError("影响事件不存在") from None
        if event.statement is not None:
            raise LeagueError("影响说明已提交")
        if not explanation.strip():
            raise LeagueError("影响说明不能为空")
        event.statement = ImpactStatement(
            bool(points_affected), bool(qualification_affected), explanation.strip(), by.actor_id
        )
        return event

    def pending_impact_events(self) -> list[ImpactEvent]:
        """秘书处尚未说明影响的事件。"""
        return [e for e in self._impact_events.values() if e.statement is None]

    # ------------------------------------------------------------------
    # 角色视图：裁判、计时员、队伍工作人员可见范围不同
    # ------------------------------------------------------------------

    def race_view(self, race_id: str, by: Actor) -> dict:
        race = self._race(race_id)
        view: dict = {
            "race_id": race.race_id,
            "station": race.station,
            "group": race.group.value,
            "round": race.round_name,
            "status": race.status.value,
            "lanes": dict(sorted(race.lanes.items())),
            "boats": dict(race.boats),
            "points_locked": race.points_locked,
        }
        if race.result is not None:
            view["result"] = {"receipt_id": race.result.receipt_id, "times": dict(race.result.times)}
        if by.role in (Role.ADMIN, Role.REFEREE):
            view["appeals"] = [self._appeal_dict(a) for a in self._appeals.values() if a.race_id == race_id]
            view["conflicts"] = [c.receipt_id for c in race.conflicts]
            view["impact_events"] = [
                e.event_id for e in self._impact_events.values() if e.race_id == race_id
            ]
        elif by.role is Role.TEAM_STAFF:
            if by.team_id is None:
                raise PermissionDenied("队伍工作人员须关联队伍")
            own_entries = {e.entry_id for e in self._entries.values() if e.team_id == by.team_id}
            view["appeals"] = [
                self._appeal_dict(a)
                for a in self._appeals.values()
                if a.race_id == race_id and a.entry_id in own_entries
            ]
        # 计时员仅见场次、赛道与成绩，不见申诉与冲突。
        return view

    @staticmethod
    def _appeal_dict(appeal: Appeal) -> dict:
        return {
            "appeal_id": appeal.appeal_id,
            "entry_id": appeal.entry_id,
            "reason": appeal.reason,
            "status": appeal.status.value,
        }

    # ------------------------------------------------------------------
    # 晋级解释：积分来源、组别资格、换人记录、未决申诉
    # ------------------------------------------------------------------

    def explain_qualification(self, group: GroupCode, through_station: int | None = None) -> list[dict]:
        """按总积分排序解释每支队伍的晋级依据，用于年度总决赛分组。"""
        through = through_station if through_station is not None else self.station_count
        self._check_station(through)
        reports: list[dict] = []
        for team in self._teams.values():
            entries = [
                e
                for e in self._entries.values()
                if e.team_id == team.team_id and e.group is group and e.station <= through
            ]
            if not entries:
                continue
            entry_by_id = {e.entry_id: e for e in entries}
            sources: list[dict] = []
            total = 0
            for station in range(1, through + 1):
                version = self._effective.get((station, group))
                if version is None:
                    continue
                for race_pts in version.race_points:
                    for entry_id in entry_by_id:
                        if entry_id not in race_pts.points:
                            continue
                        sources.append(
                            {
                                "station": station,
                                "race_id": race_pts.race_id,
                                "round": race_pts.round_name,
                                "time": race_pts.times[entry_id],
                                "rank": race_pts.ranks[entry_id],
                                "points": race_pts.points[entry_id],
                                "version": version.version,
                            }
                        )
                        total += race_pts.points[entry_id]
            entry_ids = set(entry_by_id)
            reports.append(
                {
                    "team_id": team.team_id,
                    "team_name": team.name,
                    "group": group.value,
                    "total_points": total,
                    "points_sources": sources,
                    "group_eligibility": self._eligibility_report(group, entries),
                    "substitutions": [
                        {
                            "station": entry_by_id[s.entry_id].station if s.entry_id in entry_by_id else None,
                            "out_member_id": s.out_member_id,
                            "in_member_id": s.in_member_id,
                            "reason": s.reason,
                        }
                        for s in self._substitutions.values()
                        if s.entry_id in entry_ids
                    ],
                    "pending_appeals": [
                        {"appeal_id": a.appeal_id, "race_id": a.race_id, "reason": a.reason}
                        for a in self._appeals.values()
                        if a.status is AppealStatus.PENDING and a.entry_id in entry_ids
                    ],
                    "locked_races": sorted(
                        r.race_id
                        for r in self._races.values()
                        if r.group is group and r.points_locked and entry_ids & set(r.lanes.values())
                    ),
                    "pending_impact_statements": sorted(
                        e.event_id
                        for e in self._impact_events.values()
                        if e.statement is None
                        and (
                            e.team_id == team.team_id
                            or (e.race_id is not None and entry_ids & set(self._races[e.race_id].lanes.values()))
                        )
                    ),
                }
            )
        reports.sort(key=lambda r: (-r["total_points"], r["team_id"]))
        for rank, report in enumerate(reports, start=1):
            report["rank"] = rank
            report["finals_group"] = f"第{(rank - 1) // 4 + 1}组"
        return reports

    def _eligibility_report(self, group: GroupCode, entries: list[Entry]) -> dict:
        rule = GROUP_RULES[group]
        problems: list[str] = []
        for entry in sorted(entries, key=lambda e: e.station):
            members = [self._members[m] for m in entry.paddler_ids]
            female = sum(1 for m in members if m.gender is Gender.FEMALE)
            if rule.male_only and female:
                problems.append(f"第{entry.station}站男子公开组含女子队员")
            if female < rule.min_female:
                problems.append(f"第{entry.station}站女子{female}人，少于{rule.min_female}人")
            ineligible = [m.name for m in members if not m.eligible]
            if ineligible:
                problems.append(f"第{entry.station}站{len(ineligible)}名队员资格失效")
        return {"ok": not problems, "detail": "；".join(problems) if problems else "符合组别规则"}
