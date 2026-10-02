"""校验龙舟联赛赛事后台的业务规则。"""

import unittest

from src.league_backend import LeagueBackend
from src.league_model import (
    Actor,
    AppealStatus,
    Gender,
    GroupCode,
    LeagueError,
    PermissionDenied,
    RaceStatus,
    Role,
)

ADMIN = Actor("admin-1", Role.ADMIN)
REFEREE = Actor("ref-1", Role.REFEREE)
REFEREE_2 = Actor("ref-2", Role.REFEREE)
TIMER = Actor("timer-1", Role.TIMER)


def make_team(backend: LeagueBackend, name: str, men: int = 12, women: int = 8):
    """注册一支队伍并返回 (队伍ID, 男队员ID列表, 女队员ID列表)。"""
    team_id = backend.register_team(name)
    men_ids = [backend.register_member(team_id, f"{name}男{i + 1}", Gender.MALE) for i in range(men)]
    women_ids = [backend.register_member(team_id, f"{name}女{i + 1}", Gender.FEMALE) for i in range(women)]
    return team_id, men_ids, women_ids


def staff(team_id: str, actor_id: str = "staff-1") -> Actor:
    return Actor(actor_id, Role.TEAM_STAFF, team_id=team_id)


class RosterRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LeagueBackend()

    def test_mixed_group_requires_at_least_eight_women(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "飓风", men=13, women=7)
        with self.assertRaisesRegex(LeagueError, "女子队员不得少于8人"):
            self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)

    def test_mixed_group_accepts_exactly_eight_women(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "飓风")
        entry_id = self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
        self.assertTrue(entry_id.startswith("E-"))

    def test_men_open_rejects_female_and_wrong_count(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "蛟龙", men=20, women=2)
        with self.assertRaisesRegex(LeagueError, "男子公开组不允许女子队员"):
            self.backend.enter_station(team_id, 4, GroupCode.MEN_OPEN, men_ids[:19] + women_ids[:1])
        with self.assertRaisesRegex(LeagueError, "须为20名划手"):
            self.backend.enter_station(team_id, 4, GroupCode.MEN_OPEN, men_ids[:19])

    def test_ineligible_or_foreign_member_cannot_enter(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "飓风")
        other_id, other_men, _ = make_team(self.backend, "蛟龙")
        self.backend.set_member_eligibility(men_ids[0], False, "资格赛缺席", ADMIN)
        with self.assertRaisesRegex(LeagueError, "资格无效"):
            self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
        with self.assertRaisesRegex(LeagueError, "不属于该队伍"):
            self.backend.enter_station(team_id, 3, GroupCode.MIXED, men_ids[1:12] + [other_men[0]] + women_ids)
        with self.assertRaises(PermissionDenied):
            self.backend.set_member_eligibility(men_ids[1], False, "越权", REFEREE)

    def test_duplicate_entry_rejected(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "飓风")
        self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
        with self.assertRaisesRegex(LeagueError, "已报名"):
            self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)


class SubstitutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LeagueBackend()
        self.team_id, self.men_ids, self.women_ids = make_team(self.backend, "飓风")
        self.entry_id = self.backend.enter_station(
            self.team_id, 4, GroupCode.MIXED, self.men_ids + self.women_ids
        )

    def test_roster_frozen_only_after_check_in(self) -> None:
        reserve = self.backend.register_member(self.team_id, "飓风替补1", Gender.MALE)
        with self.assertRaisesRegex(LeagueError, "检录冻结后"):
            self.backend.substitute(self.entry_id, self.men_ids[0], reserve, "伤病", staff(self.team_id))
        self.backend.check_in(self.entry_id, REFEREE)
        with self.assertRaisesRegex(LeagueError, "换人理由无效"):
            self.backend.substitute(self.entry_id, self.men_ids[0], reserve, "状态不好", staff(self.team_id))

    def test_valid_substitution_keeps_original_membership(self) -> None:
        self.backend.check_in(self.entry_id, ADMIN)
        reserve = self.backend.register_member(self.team_id, "飓风替补1", Gender.MALE)
        sub_id = self.backend.substitute(self.entry_id, self.men_ids[0], reserve, "伤病", staff(self.team_id))
        self.assertTrue(sub_id.startswith("S-"))
        # 原成员关系保留：被换下队员仍属于原队伍。
        self.assertEqual(self.backend._members[self.men_ids[0]].team_id, self.team_id)
        self.assertNotIn(self.men_ids[0], self.backend._entries[self.entry_id].paddler_ids)
        self.assertIn(reserve, self.backend._entries[self.entry_id].paddler_ids)

    def test_substitution_must_keep_mixed_ratio_and_eligibility(self) -> None:
        self.backend.check_in(self.entry_id, ADMIN)
        man_reserve = self.backend.register_member(self.team_id, "飓风替补男", Gender.MALE)
        with self.assertRaisesRegex(LeagueError, "女子队员不得少于8人"):
            self.backend.substitute(self.entry_id, self.women_ids[0], man_reserve, "伤病", ADMIN)
        woman_reserve = self.backend.register_member(self.team_id, "飓风替补女", Gender.FEMALE)
        self.backend.set_member_eligibility(woman_reserve, False, "待复核", ADMIN)
        with self.assertRaisesRegex(LeagueError, "替补队员资格无效"):
            self.backend.substitute(self.entry_id, self.women_ids[0], woman_reserve, "伤病", ADMIN)
        other_id, other_men, _ = make_team(self.backend, "蛟龙")
        with self.assertRaisesRegex(LeagueError, "替补须为本队队员"):
            self.backend.substitute(self.entry_id, self.men_ids[0], other_men[0], "伤病", ADMIN)
        with self.assertRaises(PermissionDenied):
            self.backend.substitute(self.entry_id, self.men_ids[0], man_reserve, "伤病", staff(other_id))

    def test_substitution_requires_secretariat_impact_statement(self) -> None:
        self.backend.check_in(self.entry_id, ADMIN)
        reserve = self.backend.register_member(self.team_id, "飓风替补1", Gender.MALE)
        sub_id = self.backend.substitute(self.entry_id, self.men_ids[0], reserve, "突发疾病", ADMIN)
        event_id = self.backend._substitutions[sub_id].impact_event_id
        self.assertIn(event_id, [e.event_id for e in self.backend.pending_impact_events()])
        with self.assertRaises(PermissionDenied):
            self.backend.file_impact_statement(event_id, False, False, "不影响", REFEREE)
        self.backend.file_impact_statement(event_id, False, False, "第四站开赛前换人，不影响积分与晋级", ADMIN)
        self.assertEqual(self.backend.pending_impact_events(), [])
        with self.assertRaisesRegex(LeagueError, "已提交"):
            self.backend.file_impact_statement(event_id, True, True, "重复说明", ADMIN)


class LaneAndResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LeagueBackend()
        self.teams = []
        for name in ("飓风", "蛟龙"):
            team_id, men_ids, women_ids = make_team(self.backend, name)
            mixed = self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
            self.backend.check_in(mixed, ADMIN)
            self.teams.append((team_id, mixed))
        self.boat = self.backend.register_boat("标准龙舟1号")
        self.race_id = self.backend.schedule_race(
            4,
            GroupCode.MIXED,
            "决赛",
            {1: self.teams[0][1], 2: self.teams[1][1]},
            {self.teams[0][1]: self.boat, self.teams[1][1]: self.boat},
            ADMIN,
        )
        self.times = {self.teams[0][1]: 125.4, self.teams[1][1]: 128.9}

    def test_schedule_requires_checked_in_entries(self) -> None:
        team_id, men_ids, women_ids = make_team(self.backend, "飞鱼")
        entry_id = self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
        with self.assertRaisesRegex(LeagueError, "未检录冻结"):
            self.backend.schedule_race(4, GroupCode.MIXED, "预赛", {3: entry_id}, {}, ADMIN)
        with self.assertRaises(PermissionDenied):
            self.backend.schedule_race(4, GroupCode.MIXED, "预赛", {3: entry_id}, {}, REFEREE)

    def test_lane_adjustment_records_impact_and_blocks_finished_race(self) -> None:
        event_id = self.backend.adjust_lane(self.race_id, self.teams[0][1], 3, ADMIN)
        self.assertIn(event_id, [e.event_id for e in self.backend.pending_impact_events()])
        self.assertEqual(self.backend._races[self.race_id].lanes[3], self.teams[0][1])
        self.backend.submit_result(self.race_id, {self.teams[0][1]: 125.4, self.teams[1][1]: 128.9}, TIMER)
        with self.assertRaisesRegex(LeagueError, "不能调整赛道"):
            self.backend.adjust_lane(self.race_id, self.teams[0][1], 4, ADMIN)

    def test_repeated_submission_returns_original_receipt(self) -> None:
        first = self.backend.submit_result(self.race_id, self.times, TIMER)
        replay = self.backend.submit_result(self.race_id, dict(self.times), TIMER)
        self.assertTrue(first.accepted)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt_id, first.receipt_id)
        self.assertEqual(replay.times, first.times)
        version = self.backend.settle(4, GroupCode.MIXED, ADMIN)
        again = self.backend.submit_result(self.race_id, dict(self.times), REFEREE)
        self.assertEqual(again.receipt_id, first.receipt_id)
        self.assertIs(self.backend.settle(4, GroupCode.MIXED, ADMIN), version)

    def test_conflicting_submission_locks_points_but_other_groups_settle(self) -> None:
        self.backend.submit_result(self.race_id, self.times, TIMER)
        conflict = self.backend.submit_result(
            self.race_id, {self.teams[0][1]: 130.0, self.teams[1][1]: 128.9}, TIMER
        )
        self.assertTrue(conflict.conflict)
        self.assertTrue(conflict.points_locked)
        self.assertEqual(self.backend._races[self.race_id].status, RaceStatus.CONFLICT)
        # 同一冲突内容重复提交也幂等。
        again = self.backend.submit_result(
            self.race_id, {self.teams[0][1]: 130.0, self.teams[1][1]: 128.9}, TIMER
        )
        self.assertEqual(again.receipt_id, conflict.receipt_id)
        # 混合组该场被锁定，男子公开组照常结算。
        men_entries = []
        for name in ("猛龙", "破浪"):
            team_id, men_ids, _ = make_team(self.backend, name, men=20, women=0)
            entry_id = self.backend.enter_station(team_id, 4, GroupCode.MEN_OPEN, men_ids)
            self.backend.check_in(entry_id, ADMIN)
            men_entries.append(entry_id)
        men_race = self.backend.schedule_race(
            4, GroupCode.MEN_OPEN, "决赛", {1: men_entries[0], 2: men_entries[1]}, {}, ADMIN
        )
        self.backend.submit_result(men_race, {men_entries[0]: 121.0, men_entries[1]: 123.5}, TIMER)
        mixed_version = self.backend.settle(4, GroupCode.MIXED, ADMIN)
        self.assertEqual(mixed_version.points, {})
        self.assertEqual(mixed_version.locked_races, (self.race_id,))
        men_version = self.backend.settle(4, GroupCode.MEN_OPEN, ADMIN)
        self.assertEqual(men_version.points[men_entries[0]], 10)
        self.assertEqual(men_version.points[men_entries[1]], 8)

    def test_result_correction_unlocks_and_creates_new_version(self) -> None:
        self.backend.submit_result(self.race_id, self.times, TIMER)
        first_version = self.backend.settle(4, GroupCode.MIXED, ADMIN)
        corrected = self.backend.correct_result(
            self.race_id, {self.teams[0][1]: 129.9, self.teams[1][1]: 128.9}, "计时设备校准", REFEREE
        )
        self.assertTrue(corrected.accepted)
        self.assertFalse(self.backend._races[self.race_id].points_locked)
        self.assertEqual(len(self.backend.pending_impact_events()), 1)
        second_version = self.backend.settle(4, GroupCode.MIXED, ADMIN)
        self.assertEqual(second_version.version, first_version.version + 1)
        self.assertEqual(second_version.points[self.teams[1][1]], 10)
        self.assertEqual(second_version.points[self.teams[0][1]], 8)
        with self.assertRaises(PermissionDenied):
            self.backend.correct_result(self.race_id, self.times, "越权", TIMER)

    def test_result_submission_roles_and_payload(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.backend.submit_result(self.race_id, self.times, staff(self.teams[0][0]))
        with self.assertRaisesRegex(LeagueError, "覆盖该场次全部报名"):
            self.backend.submit_result(self.race_id, {self.teams[0][1]: 125.4}, TIMER)
        with self.assertRaisesRegex(LeagueError, "成绩时间无效"):
            self.backend.submit_result(self.race_id, {self.teams[0][1]: -1.0, self.teams[1][1]: 128.9}, TIMER)


class AppealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LeagueBackend()
        self.teams = []
        for name in ("飓风", "蛟龙"):
            team_id, men_ids, women_ids = make_team(self.backend, name)
            entry_id = self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
            self.backend.check_in(entry_id, ADMIN)
            self.teams.append((team_id, entry_id))
        self.race_id = self.backend.schedule_race(
            4, GroupCode.MIXED, "决赛", {1: self.teams[0][1], 2: self.teams[1][1]}, {}, ADMIN
        )
        # 成绩由裁判 ref-1 提交。
        self.backend.submit_result(self.race_id, {self.teams[0][1]: 125.4, self.teams[1][1]: 128.9}, REFEREE)

    def test_result_submitter_cannot_confirm_dispute_alone(self) -> None:
        appeal_id = self.backend.file_appeal(
            self.race_id, self.teams[1][1], "对手越道影响成绩", staff(self.teams[1][0])
        )
        # 提交成绩的裁判不能独自确认该场争议，须由另一名裁判或秘书处处理。
        with self.assertRaisesRegex(PermissionDenied, "不能独自确认争议"):
            self.backend.decide_appeal(appeal_id, True, REFEREE)
        with self.assertRaises(PermissionDenied):
            self.backend.decide_appeal(appeal_id, True, TIMER)
        appeal = self.backend.decide_appeal(appeal_id, True, REFEREE_2)
        self.assertEqual(appeal.status, AppealStatus.UPHELD)
        with self.assertRaisesRegex(LeagueError, "已处理"):
            self.backend.decide_appeal(appeal_id, False, ADMIN)

    def test_appeal_roles_and_pending_visibility(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.backend.file_appeal(self.race_id, self.teams[0][1], "越权申诉", TIMER)
        with self.assertRaises(PermissionDenied):
            self.backend.file_appeal(self.race_id, self.teams[0][1], "他队代诉", staff(self.teams[1][0]))
        appeal_id = self.backend.file_appeal(self.race_id, self.teams[0][1], "起航犯规", REFEREE_2)
        report = self.backend.explain_qualification(GroupCode.MIXED)
        pending = {a["appeal_id"] for r in report for a in r["pending_appeals"]}
        self.assertIn(appeal_id, pending)


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LeagueBackend()
        self.teams = []
        for name in ("飓风", "蛟龙"):
            team_id, men_ids, women_ids = make_team(self.backend, name)
            entry_id = self.backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
            self.backend.check_in(entry_id, ADMIN)
            self.teams.append((team_id, entry_id))
        self.race_id = self.backend.schedule_race(
            4, GroupCode.MIXED, "决赛", {1: self.teams[0][1], 2: self.teams[1][1]}, {}, ADMIN
        )
        self.backend.submit_result(self.race_id, {self.teams[0][1]: 125.4, self.teams[1][1]: 128.9}, TIMER)
        self.backend.file_appeal(self.race_id, self.teams[0][1], "对方抢航", staff(self.teams[0][0]))

    def test_timer_sees_results_but_not_appeals(self) -> None:
        view = self.backend.race_view(self.race_id, TIMER)
        self.assertIn("result", view)
        self.assertNotIn("appeals", view)
        self.assertNotIn("conflicts", view)

    def test_referee_sees_appeals_and_conflicts(self) -> None:
        view = self.backend.race_view(self.race_id, REFEREE)
        self.assertEqual(len(view["appeals"]), 1)
        self.assertEqual(view["conflicts"], [])

    def test_team_staff_sees_only_own_appeals(self) -> None:
        own = self.backend.race_view(self.race_id, staff(self.teams[0][0]))
        self.assertEqual(len(own["appeals"]), 1)
        other = self.backend.race_view(self.race_id, staff(self.teams[1][0]))
        self.assertEqual(other["appeals"], [])
        self.assertNotIn("conflicts", other)
        with self.assertRaises(PermissionDenied):
            self.backend.race_view(self.race_id, Actor("staff-x", Role.TEAM_STAFF))


class QualificationTest(unittest.TestCase):
    def test_explanation_covers_points_eligibility_substitutions_and_appeals(self) -> None:
        backend = LeagueBackend()
        teams = {}
        for name in ("飓风", "蛟龙"):
            team_id, men_ids, women_ids = make_team(backend, name)
            teams[name] = (team_id, men_ids, women_ids)
        # 两个分站的成绩：飓风两次第一。
        race_ids = {}
        entries_by_station = {}
        for station in (3, 4):
            entries = {}
            for name, (team_id, men_ids, women_ids) in teams.items():
                entry_id = backend.enter_station(team_id, station, GroupCode.MIXED, men_ids + women_ids)
                backend.check_in(entry_id, ADMIN)
                entries[name] = entry_id
            entries_by_station[station] = entries
            race_id = backend.schedule_race(
                station, GroupCode.MIXED, "决赛", {1: entries["飓风"], 2: entries["蛟龙"]}, {}, ADMIN
            )
            backend.submit_result(race_id, {entries["飓风"]: 124.0, entries["蛟龙"]: 127.0}, TIMER)
            backend.settle(station, GroupCode.MIXED, ADMIN)
            race_ids[station] = race_id
        # 第四站检录后换人：保留原成员关系并产生待说明事件。
        jiaolong_entry = entries_by_station[4]["蛟龙"]
        out_member = teams["蛟龙"][1][0]
        reserve = backend.register_member(teams["蛟龙"][0], "蛟龙替补1", Gender.MALE)
        backend.substitute(jiaolong_entry, out_member, reserve, "伤病", ADMIN)
        # 蛟龙对第四站成绩提出申诉，尚未处理。
        appeal_id = backend.file_appeal(race_ids[4], jiaolong_entry, "对方越道", staff(teams["蛟龙"][0]))
        report = backend.explain_qualification(GroupCode.MIXED)
        self.assertEqual([r["team_name"] for r in report], ["飓风", "蛟龙"])
        leader, runner_up = report
        self.assertEqual(leader["total_points"], 20)
        self.assertEqual(runner_up["total_points"], 16)
        self.assertEqual(leader["rank"], 1)
        self.assertEqual(leader["finals_group"], "第1组")
        self.assertEqual(len(leader["points_sources"]), 2)
        self.assertTrue(all(s["points"] == 10 for s in leader["points_sources"]))
        self.assertTrue(leader["group_eligibility"]["ok"])
        self.assertEqual(leader["substitutions"], [])
        self.assertEqual(leader["pending_appeals"], [])
        # 蛟龙的解释包含换人记录、未决申诉与秘书处待说明事件。
        self.assertEqual(len(runner_up["substitutions"]), 1)
        self.assertEqual(runner_up["substitutions"][0]["out_member_id"], out_member)
        self.assertEqual(runner_up["substitutions"][0]["in_member_id"], reserve)
        self.assertEqual(runner_up["substitutions"][0]["reason"], "伤病")
        self.assertEqual(runner_up["substitutions"][0]["station"], 4)
        self.assertEqual([a["appeal_id"] for a in runner_up["pending_appeals"]], [appeal_id])
        self.assertEqual(len(runner_up["pending_impact_statements"]), 1)
        self.assertEqual(leader["pending_impact_statements"], [])

    def test_eligibility_flags_ratio_and_ineligible_members(self) -> None:
        backend = LeagueBackend()
        team_id, men_ids, women_ids = make_team(backend, "飓风")
        entry_id = backend.enter_station(team_id, 4, GroupCode.MIXED, men_ids + women_ids)
        backend.check_in(entry_id, ADMIN)
        backend.set_member_eligibility(men_ids[0], False, "转会审核中", ADMIN)
        report = backend.explain_qualification(GroupCode.MIXED)
        eligibility = report[0]["group_eligibility"]
        self.assertFalse(eligibility["ok"])
        self.assertIn("资格失效", eligibility["detail"])


if __name__ == "__main__":
    unittest.main()
