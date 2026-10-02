"""联赛后台核心服务：把领域规则落成可调用的操作。

所有写操作都：
- 先做角色权限校验（permissions）；
- 再做业务规则校验；
- 涉及临时换人、赛道调整、成绩更正时，强制记录积分与晋级影响说明。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Callable, Optional

from .errors import (
    NotFoundError,
    PermissionDeniedError,
    PointsLockedError,
    RosterFrozenError,
    ValidationError,
)
from .models import (
    Appeal,
    AppealStatus,
    Boat,
    Category,
    CheckIn,
    CheckInRow,
    Entry,
    EntryStatus,
    LaneAssignment,
    LedgerEntry,
    LedgerStatus,
    Membership,
    MembershipStatus,
    Person,
    Position,
    Race,
    RaceStatus,
    ResultLine,
    ResultSubmission,
    Stage,
    SubReason,
    Substitution,
    SubstitutionStatus,
    Team,
    User,
    VALID_SUB_REASONS,
)
from . import permissions as perm
from .rules import check_composition, points_for_place
from .store import Store


class LeagueService:
    def __init__(
        self, store: Optional[Store] = None, clock: Optional[Callable[[], str]] = None
    ) -> None:
        self.store = store or Store()
        self.clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))

    # ---------- 基础读取 ----------

    def _user(self, uid: str) -> User:
        user = self.store.users.get(uid)
        if user is None:
            raise NotFoundError(f"用户不存在：{uid}")
        return user

    def _team(self, tid: str) -> Team:
        team = self.store.teams.get(tid)
        if team is None:
            raise NotFoundError(f"队伍不存在：{tid}")
        return team

    def _entry(self, eid: str) -> Entry:
        entry = self.store.entries.get(eid)
        if entry is None:
            raise NotFoundError(f"报名不存在：{eid}")
        return entry

    def _race(self, rid: str) -> Race:
        race = self.store.races.get(rid)
        if race is None:
            raise NotFoundError(f"场次不存在：{rid}")
        return race

    def _audit(
        self, actor: str, action: str, target: str, detail: str,
        points_affected: bool, qualification_affected: bool,
    ) -> None:
        from .models import AuditEvent

        self.store.audit.append(
            AuditEvent(
                eid=self.store.next_id("AE"),
                at=self.clock(),
                actor=actor,
                action=action,
                target=target,
                detail=detail,
                points_affected=points_affected,
                qualification_affected=qualification_affected,
            )
        )

    # ---------- 用户、队伍、队员、资格 ----------

    def register_user(
        self, actor_id: str, uid: str, name: str, role, team_id: Optional[str] = None
    ) -> User:
        from .models import Role

        if actor_id in self.store.users:
            actor = self._user(actor_id)
            perm.require(actor.role, "team.register")
        else:
            # 引导：系统中第一个用户必须是秘书处，之后的用户都要经有权角色创建。
            if self.store.users or role != Role.SECRETARIAT:
                raise PermissionDeniedError("只有秘书处可以登记用户")
        if role == Role.TEAM_STAFF and team_id is None:
            raise ValidationError("队伍工作人员必须归属一支队伍")
        if team_id is not None and team_id not in self.store.teams:
            raise NotFoundError(f"队伍不存在：{team_id}")
        if uid in self.store.users:
            raise ValidationError(f"用户已存在：{uid}")
        user = User(uid=uid, name=name, role=role, team_id=team_id)
        self.store.users[uid] = user
        if team_id is not None:
            self._team(team_id).staff_user_ids.append(uid)
        return user

    def register_team(self, actor_id: str, tid: str, name: str) -> Team:
        actor = self._user(actor_id)
        perm.require(actor.role, "team.register")
        if tid in self.store.teams:
            raise ValidationError(f"队伍已存在：{tid}")
        team = Team(tid=tid, name=name)
        self.store.teams[tid] = team
        return team

    def register_person(self, actor_id: str, pid: str, name: str, gender) -> Person:
        actor = self._user(actor_id)
        perm.require(actor.role, "person.register")
        if pid in self.store.persons:
            raise ValidationError(f"队员已存在：{pid}")
        person = Person(pid=pid, name=name, gender=gender)
        self.store.persons[pid] = person
        return person

    def add_membership(
        self, actor_id: str, team_id: str, person_id: str, position: Position
    ) -> Membership:
        actor = self._user(actor_id)
        perm.require(actor.role, "membership.verify")
        self._team(team_id)
        if person_id not in self.store.persons:
            raise NotFoundError(f"队员不存在：{person_id}")
        for m in self.store.memberships.values():
            if (
                m.person_id == person_id
                and m.team_id == team_id
                and m.status != MembershipStatus.REMOVED
            ):
                raise ValidationError("该队员已在队中")
        mid = self.store.next_id("M")
        membership = Membership(mid=mid, team_id=team_id, person_id=person_id, position=position)
        self.store.memberships[mid] = membership
        return membership

    def verify_membership(self, actor_id: str, membership_id: str, eligible: bool) -> Membership:
        """秘书处确认队员资格（注册、年龄、跨队等审核通过后才能上场）。"""

        actor = self._user(actor_id)
        perm.require(actor.role, "membership.verify")
        membership = self.store.memberships.get(membership_id)
        if membership is None:
            raise NotFoundError(f"成员关系不存在：{membership_id}")
        membership.eligible = eligible
        membership.verified_by = actor.uid
        membership.verified_at = self.clock()
        return membership

    def team_memberships(self, team_id: str) -> list[Membership]:
        return [m for m in self.store.memberships.values() if m.team_id == team_id]

    # ---------- 分站、船只、报名 ----------

    def create_stage(self, sid: str, name: str, seq: int) -> Stage:
        stage = Stage(sid=sid, name=name, seq=seq)
        self.store.stages[sid] = stage
        return stage

    def register_boat(self, actor_id: str, bid: str, label: str) -> Boat:
        actor = self._user(actor_id)
        perm.require(actor.role, "race.arrange")
        boat = Boat(bid=bid, label=label)
        self.store.boats[bid] = boat
        return boat

    def create_entry(self, actor_id: str, stage_id: str, team_id: str, category: Category) -> Entry:
        actor = self._user(actor_id)
        perm.require(actor.role, "entry.create")
        perm.ensure_team_scope(actor.role, actor.team_id, team_id)
        if stage_id not in self.store.stages:
            raise NotFoundError(f"分站不存在：{stage_id}")
        for e in self.store.entries.values():
            if e.stage_id == stage_id and e.team_id == team_id and e.category == category:
                raise ValidationError("该队在该分站该组别已报名")
        entry = Entry(
            eid=self.store.next_id("E"),
            stage_id=stage_id,
            team_id=team_id,
            category=category,
            created_by=actor.uid,
        )
        self.store.entries[entry.eid] = entry
        return entry

    # ---------- 检录与冻结 ----------

    def latest_checkin(self, entry_id: str) -> Optional[CheckIn]:
        versions = [c for c in self.store.checkins.values() if c.entry_id == entry_id]
        if not versions:
            return None
        return max(versions, key=lambda c: c.version)

    def freeze_checkin(self, actor_id: str, entry_id: str, person_ids: list[str]) -> CheckIn:
        """检录通过后冻结名单；此后只能走替补流程。"""

        actor = self._user(actor_id)
        perm.require(actor.role, "checkin.freeze")
        entry = self._entry(entry_id)
        if entry.status != EntryStatus.ENTERED:
            raise ValidationError("队伍已退赛，不能检录")

        by_person: dict[str, Membership] = {}
        for m in self.store.memberships.values():
            if m.team_id == entry.team_id and m.status == MembershipStatus.ACTIVE:
                by_person[m.person_id] = m

        rows: list[CheckInRow] = []
        seen: set[str] = set()
        for pid in person_ids:
            if pid in seen:
                raise ValidationError(f"名单中队员重复：{pid}")
            seen.add(pid)
            membership = by_person.get(pid)
            if membership is None:
                raise ValidationError(f"队员不在队或已被替补：{pid}")
            if not membership.eligible:
                raise ValidationError(f"队员资格未经秘书处确认：{pid}")
            person = self.store.persons[pid]
            rows.append(
                CheckInRow(
                    membership_id=membership.mid,
                    person_id=pid,
                    person_name=person.name,
                    gender=person.gender,
                    position=membership.position,
                )
            )

        violations = check_composition(rows, entry.category)
        if self.latest_checkin(entry_id) is not None:
            raise RosterFrozenError("名单已冻结，换人须提交替补申请")
        checkin = CheckIn(
            cid=self.store.next_id("C"),
            entry_id=entry_id,
            version=1,
            rows=rows,
            paddler_count=sum(1 for r in rows if r.position == Position.PADDLER),
            female_paddlers=sum(
                1 for r in rows if r.position == Position.PADDLER and r.gender.value == "女"
            ),
            compliant=not violations,
            violations=violations,
            frozen_by=actor.uid,
            frozen_at=self.clock(),
        )
        if violations:
            raise ValidationError("；".join(violations))
        self.store.checkins[checkin.cid] = checkin
        return checkin

    # ---------- 替补（冻结后换人） ----------

    def request_substitution(
        self,
        actor_id: str,
        entry_id: str,
        original_person_id: str,
        replacement_person_id: str,
        reason: SubReason,
        reason_detail: str = "",
    ) -> Substitution:
        actor = self._user(actor_id)
        perm.require(actor.role, "substitution.request")
        entry = self._entry(entry_id)
        perm.ensure_team_scope(actor.role, actor.team_id, entry.team_id)
        checkin = self.latest_checkin(entry_id)
        if checkin is None:
            raise ValidationError("尚未检录冻结，无需替补")
        if reason not in VALID_SUB_REASONS:
            raise ValidationError("替补理由无效")
        if reason == SubReason.OTHER and not reason_detail.strip():
            raise ValidationError("理由为“其他”时必须填写具体说明")

        frozen_ids = {r.person_id for r in checkin.rows}
        if original_person_id not in frozen_ids:
            raise ValidationError("被替换队员不在冻结名单中")
        if replacement_person_id in frozen_ids:
            raise ValidationError("替补队员已在冻结名单中")

        original = next(
            m
            for m in self.store.memberships.values()
            if m.team_id == entry.team_id and m.person_id == original_person_id
        )
        replacement = next(
            (
                m
                for m in self.store.memberships.values()
                if m.team_id == entry.team_id
                and m.person_id == replacement_person_id
                and m.status == MembershipStatus.ACTIVE
            ),
            None,
        )
        if replacement is None:
            raise ValidationError("替补队员不是本队在队成员")
        if not replacement.eligible:
            raise ValidationError("替补队员资格未经秘书处确认")
        if replacement.position != original.position:
            raise ValidationError("替补队员位置必须与被替换队员一致（划手换划手）")

        sub = Substitution(
            sid=self.store.next_id("S"),
            entry_id=entry_id,
            team_id=entry.team_id,
            original_membership_id=original.mid,
            original_person_id=original_person_id,
            replacement_person_id=replacement_person_id,
            position=original.position,
            reason=reason,
            reason_detail=reason_detail,
            requested_by=actor.uid,
            requested_at=self.clock(),
        )
        self.store.substitutions[sub.sid] = sub
        return sub

    def review_substitution(
        self,
        actor_id: str,
        sub_id: str,
        approve: bool,
        points_affected: Optional[bool] = None,
        qualification_affected: Optional[bool] = None,
        impact_note: str = "",
    ) -> Substitution:
        """秘书处审批替补；批准即生成新一版冻结名单，并必须说明积分/晋级影响。"""

        actor = self._user(actor_id)
        perm.require(actor.role, "substitution.review")
        sub = self.store.substitutions.get(sub_id)
        if sub is None:
            raise NotFoundError(f"替补申请不存在：{sub_id}")
        if sub.status != SubstitutionStatus.PENDING:
            raise ValidationError("该替补申请已处理")

        sub.reviewed_by = actor.uid
        sub.reviewed_at = self.clock()
        if points_affected is None or qualification_affected is None or not impact_note.strip():
            raise ValidationError("审批必须说明积分是否受影响、晋级是否受影响及依据")
        sub.points_affected = points_affected
        sub.qualification_affected = qualification_affected
        sub.impact_note = impact_note

        if not approve:
            sub.status = SubstitutionStatus.REJECTED
            self._audit(
                actor.uid, "替补驳回", sub.sid,
                f"驳回 {sub.original_person_id} 的替补申请；{impact_note}",
                points_affected, qualification_affected,
            )
            return sub

        entry = self._entry(sub.entry_id)
        checkin = self.latest_checkin(sub.entry_id)
        assert checkin is not None
        new_rows = [
            CheckInRow(
                membership_id=(
                    next(
                        m.mid
                        for m in self.store.memberships.values()
                        if m.team_id == entry.team_id
                        and m.person_id == sub.replacement_person_id
                    )
                    if r.person_id == sub.original_person_id
                    else r.membership_id
                ),
                person_id=(
                    sub.replacement_person_id
                    if r.person_id == sub.original_person_id
                    else r.person_id
                ),
                person_name=(
                    self.store.persons[sub.replacement_person_id].name
                    if r.person_id == sub.original_person_id
                    else r.person_name
                ),
                gender=(
                    self.store.persons[sub.replacement_person_id].gender
                    if r.person_id == sub.original_person_id
                    else r.gender
                ),
                position=r.position,
            )
            for r in checkin.rows
        ]
        violations = check_composition(new_rows, entry.category)
        if violations:
            sub.status = SubstitutionStatus.REJECTED
            raise ValidationError("替补后名单不再合规，驳回：" + "；".join(violations))

        # 保留原成员关系：只改状态，不删除。
        original_membership = self.store.memberships[sub.original_membership_id]
        original_membership.status = MembershipStatus.REPLACED

        new_checkin = CheckIn(
            cid=self.store.next_id("C"),
            entry_id=sub.entry_id,
            version=checkin.version + 1,
            rows=new_rows,
            paddler_count=sum(1 for r in new_rows if r.position == Position.PADDLER),
            female_paddlers=sum(
                1
                for r in new_rows
                if r.position == Position.PADDLER and r.gender.value == "女"
            ),
            compliant=True,
            violations=[],
            frozen_by=actor.uid,
            frozen_at=self.clock(),
        )
        self.store.checkins[new_checkin.cid] = new_checkin
        sub.status = SubstitutionStatus.APPROVED
        self._audit(
            actor.uid, "替补批准", sub.sid,
            f"{sub.original_person_id}→{sub.replacement_person_id}（{sub.reason.value}）；{impact_note}",
            points_affected, qualification_affected,
        )
        return sub

    # ---------- 场次、赛道与船只 ----------

    def arrange_race(
        self, actor_id: str, rid: str, stage_id: str, category: Category, round_name: str
    ) -> Race:
        actor = self._user(actor_id)
        perm.require(actor.role, "race.arrange")
        if stage_id not in self.store.stages:
            raise NotFoundError(f"分站不存在：{stage_id}")
        race = Race(rid=rid, stage_id=stage_id, category=category, round_name=round_name)
        self.store.races[rid] = race
        self.store.lanes[rid] = []
        self.store.results[rid] = []
        self.store.ledger[rid] = []
        return race

    def assign_lane(
        self,
        actor_id: str,
        race_id: str,
        team_id: str,
        lane: int,
        boat_id: str,
    ) -> LaneAssignment:
        actor = self._user(actor_id)
        perm.require(actor.role, "race.arrange")
        race = self._race(race_id)
        if race.status != RaceStatus.ARRANGED:
            raise ValidationError("已接收成绩后不能重新分配赛道")
        self._team(team_id)
        if boat_id not in self.store.boats:
            raise NotFoundError(f"船只不存在：{boat_id}")
        history = self.store.lanes[race_id]
        if any(a.team_id == team_id for a in history):
            raise ValidationError("该队已分配赛道")
        if any(a.lane == lane for a in history):
            raise ValidationError(f"{lane}号赛道已占用")
        assignment = LaneAssignment(
            laid=self.store.next_id("LA"),
            race_id=race_id,
            team_id=team_id,
            lane=lane,
            boat_id=boat_id,
            version=len(history) + 1,
            reason="初次编排",
            changed_by=actor.uid,
            changed_at=self.clock(),
            points_affected=False,
            qualification_affected=False,
            impact_note="初次编排，不影响积分与晋级",
        )
        history.append(assignment)
        return assignment

    def adjust_lane(
        self,
        actor_id: str,
        race_id: str,
        team_id: str,
        new_lane: int,
        new_boat_id: Optional[str],
        reason: str,
        points_affected: bool,
        qualification_affected: bool,
        impact_note: str,
    ) -> LaneAssignment:
        """临时赛道/船只调整；秘书处必须留下理由与积分、晋级影响说明。"""

        actor = self._user(actor_id)
        perm.require(actor.role, "lane.adjust")
        race = self._race(race_id)
        if race.status in (RaceStatus.SETTLED, RaceStatus.LOCKED):
            raise ValidationError("场次已结算或锁定，不能调整赛道")
        if not reason.strip() or not impact_note.strip():
            raise ValidationError("赛道调整必须填写理由与影响说明")
        history = self.store.lanes[race_id]
        current = next((a for a in reversed(history) if a.team_id == team_id), None)
        if current is None:
            raise NotFoundError("该队尚未分配赛道")
        boat_id = new_boat_id or current.boat_id
        if boat_id not in self.store.boats:
            raise NotFoundError(f"船只不存在：{boat_id}")
        # 同一场以每队最后一条编排为准，检查赛道是否撞号。
        latest_by_team = {}
        for a in history:
            latest_by_team[a.team_id] = a
        if any(a.lane == new_lane for t, a in latest_by_team.items() if t != team_id):
            raise ValidationError(f"{new_lane}号赛道已被占用")
        assignment = LaneAssignment(
            laid=self.store.next_id("LA"),
            race_id=race_id,
            team_id=team_id,
            lane=new_lane,
            boat_id=boat_id,
            version=len(history) + 1,
            reason=reason,
            changed_by=actor.uid,
            changed_at=self.clock(),
            points_affected=points_affected,
            qualification_affected=qualification_affected,
            impact_note=impact_note,
        )
        history.append(assignment)
        self._audit(
            actor.uid, "赛道船只调整", race_id,
            f"队伍{team_id}调整至{new_lane}号赛道/船只{boat_id}（{reason}）；{impact_note}",
            points_affected, qualification_affected,
        )
        return assignment

    # ---------- 成绩接收：幂等、冲突锁定、更正版本 ----------

    @staticmethod
    def _content_hash(lines: list[ResultLine]) -> str:
        body = "|".join(
            f"{t}:{ms}:{p}"
            for t, ms, p in sorted(
                ((l.team_id, l.finish_ms, l.place) for l in lines), key=lambda x: x[0]
            )
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _validate_lines(self, race: Race, lines: list[ResultLine]) -> None:
        if not lines:
            raise ValidationError("成绩内容为空")
        teams = [l.team_id for l in lines]
        if len(set(teams)) != len(teams):
            raise ValidationError("成绩中存在重复队伍")
        places = [l.place for l in lines]
        if sorted(places) != list(range(1, len(lines) + 1)):
            raise ValidationError("名次必须从1连续编号")
        if any(l.finish_ms <= 0 for l in lines):
            raise ValidationError("计时成绩无效")
        for l in lines:
            self._team(l.team_id)

    def submit_result(
        self, actor_id: str, race_id: str, lines: list[ResultLine], note: str = ""
    ) -> tuple[ResultSubmission, str]:
        """接收一场成绩。

        返回（成绩记录, 接收结论）。结论之一：
        - duplicate：内容与已接收成绩一致，返回原结果且不做任何改动；
        - received：首次接收，待裁判确认；
        - conflict：内容与已接收成绩不一致，该场积分锁定并生成争议，其他场次/组别不受影响。
        """

        actor = self._user(actor_id)
        perm.require(actor.role, "result.submit")
        race = self._race(race_id)
        self._validate_lines(race, lines)
        digest = self._content_hash(lines)

        prior = self.store.results[race_id]
        for old in prior:
            if old.content_hash == digest:
                outcome = "duplicate"
                if race.status == RaceStatus.LOCKED:
                    outcome = "duplicate-locked"
                return old, outcome

        submission = ResultSubmission(
            rsid=self.store.next_id("RS"),
            race_id=race_id,
            lines=tuple(lines),
            content_hash=digest,
            submitted_by=actor.uid,
            submitted_at=self.clock(),
            note=note,
        )

        if prior:
            # 同一场出现内容不一致的成绩 → 锁定该场积分，自动立案争议。
            prior.append(submission)
            self._lock_race_for_conflict(race, submission, actor)
            return submission, "conflict"

        prior.append(submission)
        race.status = RaceStatus.RESULT_RECEIVED
        return submission, "received"

    def _lock_race_for_conflict(
        self, race: Race, new_submission: ResultSubmission, actor: User
    ) -> Appeal:
        race.status = RaceStatus.LOCKED
        # 已结算的积分冻结但不删除；尚未结算则无台账可冻。
        for entry in self.store.ledger[race.rid]:
            if entry.status == LedgerStatus.CONFIRMED:
                entry.status = LedgerStatus.LOCKED
        candidates = [r.rsid for r in self.store.results[race.rid]]
        existing_open = next(
            (
                a
                for a in self.store.appeals.values()
                if a.race_id == race.rid and a.status == AppealStatus.OPEN
            ),
            None,
        )
        if existing_open is None:
            appeal = Appeal(
                aid=self.store.next_id("A"),
                race_id=race.rid,
                team_id=None,
                reason="同一场成绩重复接收且内容不一致，系统自动锁定该场积分",
                raised_by=actor.uid,
                raised_at=self.clock(),
                candidate_rsids=candidates,
            )
            self.store.appeals[appeal.aid] = appeal
        else:
            existing_open.candidate_rsids = candidates
            appeal = existing_open
        self._audit(
            actor.uid, "成绩冲突锁定", race.rid,
            f"出现第{len(candidates)}个内容不同的成绩版本（{new_submission.rsid}），"
            "该场积分锁定，等待争议复核；其他组别照常结算",
            True, True,
        )
        return appeal

    def confirm_result(self, actor_id: str, race_id: str) -> list[LedgerEntry]:
        """裁判确认成绩并结算积分；确认人不能是该场成绩的提交人。"""

        actor = self._user(actor_id)
        perm.require(actor.role, "result.confirm")
        race = self._race(race_id)
        if race.status == RaceStatus.LOCKED:
            raise PointsLockedError("该场积分已锁定，须先裁决争议")
        submissions = self.store.results[race_id]
        if not submissions:
            raise ValidationError("尚未收到成绩")
        submission = submissions[-1]
        if submission.submitted_by == actor.uid:
            raise PermissionDeniedError("提交成绩的人不能独自确认，须由另一位裁判确认")
        return self._settle(race, submission, actor.uid)

    def _settle(
        self, race: Race, submission: ResultSubmission, confirmed_by: str
    ) -> list[LedgerEntry]:
        for old in self.store.ledger[race.rid]:
            if old.status in (LedgerStatus.CONFIRMED, LedgerStatus.LOCKED):
                old.status = LedgerStatus.VOIDED
        version = 1 + max(
            (e.result_version for e in self.store.ledger[race.rid]), default=0
        )
        created: list[LedgerEntry] = []
        for line in submission.lines:
            entry = LedgerEntry(
                lid=self.store.next_id("L"),
                race_id=race.rid,
                stage_id=race.stage_id,
                category=race.category,
                team_id=line.team_id,
                place=line.place,
                points=points_for_place(line.place),
                status=LedgerStatus.CONFIRMED,
                result_version=version,
                created_at=self.clock(),
                note=f"依据成绩{submission.rsid}，由{confirmed_by}确认",
            )
            self.store.ledger[race.rid].append(entry)
            created.append(entry)
        race.status = RaceStatus.SETTLED
        return created

    def correct_result(
        self,
        actor_id: str,
        race_id: str,
        lines: list[ResultLine],
        reason: str,
        points_affected: bool,
        qualification_affected: bool,
    ) -> tuple[ResultSubmission, list[LedgerEntry]]:
        """秘书处按有效依据更正成绩：产生新版本台账，并说明积分与晋级影响。

        与“重复接收冲突”不同，更正是秘书处主动发起的修订；
        争议锁定中的场次不得更正，须先裁决申诉。
        """

        actor = self._user(actor_id)
        perm.require(actor.role, "result.correct")
        race = self._race(race_id)
        if race.status == RaceStatus.LOCKED:
            raise PointsLockedError("该场积分锁定中，不能直接更正，须先裁决争议")
        if not reason.strip():
            raise ValidationError("成绩更正必须说明理由")
        self._validate_lines(race, lines)
        digest = self._content_hash(lines)

        old_total = {e.team_id: e.points for e in self.current_ledger(race_id)}
        prior = self.store.results[race_id]
        same = next((r for r in prior if r.content_hash == digest), None)
        if same is not None:
            # 更正内容与现有版本完全一致：幂等返回，不产生新版本。
            return same, self.current_ledger(race_id)

        submission = ResultSubmission(
            rsid=self.store.next_id("RS"),
            race_id=race_id,
            lines=tuple(lines),
            content_hash=digest,
            submitted_by=actor.uid,
            submitted_at=self.clock(),
            note=f"更正：{reason}",
        )
        prior.append(submission)
        # 秘书处更正以秘书处版本为准直接重算，不构成争议锁定。
        new_entries = self._settle(race, submission, actor.uid)
        new_total = {e.team_id: e.points for e in new_entries}
        self._audit(
            actor.uid, "成绩更正", race_id,
            f"理由：{reason}；积分影响：{points_affected}，晋级影响：{qualification_affected}；"
            f"变更前{old_total}，变更后{new_total}",
            points_affected, qualification_affected,
        )
        return submission, new_entries

    def current_ledger(self, race_id: str) -> list[LedgerEntry]:
        """取一场当前生效的台账行（CONFIRMED 或 LOCKED，不含 VOIDED）。"""

        return [
            e
            for e in self.store.ledger.get(race_id, [])
            if e.status in (LedgerStatus.CONFIRMED, LedgerStatus.LOCKED)
        ]

    # ---------- 申诉与双人复核 ----------

    def raise_appeal(
        self, actor_id: str, race_id: str, reason: str, team_id: Optional[str] = None
    ) -> Appeal:
        actor = self._user(actor_id)
        perm.require(actor.role, "appeal.raise")
        race = self._race(race_id)
        if team_id is not None:
            perm.ensure_team_scope(actor.role, actor.team_id, team_id)
        elif actor.role.value == "队伍工作人员":
            team_id = actor.team_id
        if not reason.strip():
            raise ValidationError("申诉必须说明理由")
        appeal = Appeal(
            aid=self.store.next_id("A"),
            race_id=race_id,
            team_id=team_id,
            reason=reason,
            raised_by=actor.uid,
            raised_at=self.clock(),
        )
        self.store.appeals[appeal.aid] = appeal
        # 申诉期间该场积分冻结。
        race.status = RaceStatus.LOCKED
        for entry in self.store.ledger[race_id]:
            if entry.status == LedgerStatus.CONFIRMED:
                entry.status = LedgerStatus.LOCKED
        return appeal

    def resolve_appeal(
        self,
        actor_id: str,
        appeal_id: str,
        uphold: bool,
        selected_rsid: Optional[str],
        resolution_note: str,
        points_affected: bool,
        qualification_affected: bool,
    ) -> Appeal:
        """秘书处裁决争议并解锁重算。

        关键约束：成绩提交人不能独自确认争议——裁决人不得是任一候选成绩的提交人。
        """

        actor = self._user(actor_id)
        perm.require(actor.role, "appeal.resolve")
        appeal = self.store.appeals.get(appeal_id)
        if appeal is None:
            raise NotFoundError(f"申诉不存在：{appeal_id}")
        if appeal.status != AppealStatus.OPEN:
            raise ValidationError("该申诉已裁决")
        if not resolution_note.strip():
            raise ValidationError("裁决必须写明依据")

        candidates = self.store.results[appeal.race_id]
        submitters = {r.submitted_by for r in candidates}
        if actor.uid in submitters:
            raise PermissionDeniedError("提交成绩的人不能独自确认争议，须由未提交成绩的秘书处人员裁决")

        if uphold:
            if selected_rsid is None:
                raise ValidationError("申诉成立时必须选定一个成绩版本")
            chosen = next((r for r in candidates if r.rsid == selected_rsid), None)
            if chosen is None:
                raise NotFoundError(f"候选成绩不存在：{selected_rsid}")
            appeal.status = AppealStatus.UPHELD
            appeal.selected_rsid = selected_rsid
            race = self._race(appeal.race_id)
            self._settle(race, chosen, actor.uid)
        else:
            appeal.status = AppealStatus.REJECTED
            race = self._race(appeal.race_id)
            # 驳回：若已有台账，恢复为已确认；若从未结算，按最早接收版本结算。
            current = self.current_ledger(appeal.race_id)
            if current:
                for e in current:
                    e.status = LedgerStatus.CONFIRMED
                race.status = RaceStatus.SETTLED
            else:
                self._settle(race, candidates[0], actor.uid)

        appeal.resolved_by = actor.uid
        appeal.resolved_at = self.clock()
        appeal.resolution_note = resolution_note
        appeal.points_changed = points_affected
        appeal.qualification_affected = qualification_affected
        self._audit(
            actor.uid, "申诉裁决", appeal.aid,
            f"结论：{appeal.status.value}；依据：{resolution_note}；"
            f"积分影响：{points_affected}，晋级影响：{qualification_affected}",
            points_affected, qualification_affected,
        )
        return appeal

    # ---------- 查询辅助 ----------

    def visible_scope(self, actor_id: str) -> str:
        actor = self._user(actor_id)
        return perm.VISIBLE_SCOPE[actor.role]

    def team_substitutions(self, team_id: str) -> list[Substitution]:
        return sorted(
            (s for s in self.store.substitutions.values() if s.team_id == team_id),
            key=lambda s: s.sid,
        )

    def open_appeals_for_team(self, team_id: str) -> list[Appeal]:
        result = []
        for appeal in self.store.appeals.values():
            if appeal.status != AppealStatus.OPEN:
                continue
            if appeal.team_id == team_id:
                result.append(appeal)
                continue
            # 场次级争议（team_id 为空）：该场台账涉及该队也算未决。
            if any(e.team_id == team_id for e in self.store.ledger.get(appeal.race_id, [])):
                result.append(appeal)
        return result

    def audit_log(self, actor_id: str):
        actor = self._user(actor_id)
        perm.require(actor.role, "audit.read")
        return list(self.store.audit)
