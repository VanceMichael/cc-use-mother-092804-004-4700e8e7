"""赛事后台业务规则测试：注册、资格、检录、换人、赛道、成绩、积分、申诉、晋级。"""

from __future__ import annotations

import unittest

from src.dragon import (
    AppealStatus,
    Category,
    Gender,
    LedgerStatus,
    LeagueService,
    Position,
    RaceStatus,
    ResultLine,
    Role,
    RosterFrozenError,
    SubReason,
    ValidationError,
    PermissionDeniedError,
    PointsLockedError,
)
from src.dragon.qualification import QualificationQuery
from src.dragon.rules import MIXED_MIN_FEMALE_PADDLERS


def _clock():
    _clock.t += 1
    return f"2026-09-20T09:{_clock.t:02d}:00"


_clock.t = 0


class LeagueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LeagueService(clock=_clock)
        # 首位秘书处用户（系统引导）
        self.svc.register_user("__bootstrap__", "S1", "秘书处甲", Role.SECRETARIAT)
        self.svc.register_user("S1", "R1", "裁判甲", Role.REFEREE)
        self.svc.register_user("S1", "R2", "裁判乙", Role.REFEREE)
        self.svc.register_user("S1", "T1", "计时员甲", Role.TIMER)

        self.svc.register_team("S1", "TA", "珠江猛龙队")
        self.svc.register_team("S1", "TB", "花城凤凰队")
        self.svc.register_team("S1", "TC", "荔湾蛟龙队")
        self.svc.register_user("S1", "STA", "猛龙工作人员", Role.TEAM_STAFF, team_id="TA")
        self.svc.register_user("S1", "STB", "凤凰工作人员", Role.TEAM_STAFF, team_id="TB")

        # 每队一个人员池：21 名男划手 + 9 名女划手
        # 男子公开组取 20 男；混合组取 12 男 8 女，各留同性别替补。
        self.roster_a = self._build_roster("TA", men=21, women=9)
        self.roster_b = self._build_roster("TB", men=21, women=9)
        self.men_a = self.roster_a
        self.men_b = self.roster_b
        self.mix_a = self.roster_a
        self.mix_b = self.roster_b

        for i in range(1, 5):
            self.svc.create_stage(f"ST{i}", f"第{i}站", i)
        self.svc.register_boat("R1", "B1", "标准龙舟1号")
        self.svc.register_boat("R1", "B2", "标准龙舟2号")

    def _build_roster(self, team_id: str, men: int, women: int) -> dict[str, list[str]]:
        pids = {"men": [], "women": []}
        for i in range(men):
            pid = f"{team_id}-M{i+1}"
            self.svc.register_person("S1", pid, f"男队员{i+1}", Gender.MALE)
            m = self.svc.add_membership("S1", team_id, pid, Position.PADDLER)
            self.svc.verify_membership("S1", m.mid, True)
            pids["men"].append(pid)
        for i in range(women):
            pid = f"{team_id}-F{i+1}"
            self.svc.register_person("S1", pid, f"女队员{i+1}", Gender.FEMALE)
            m = self.svc.add_membership("S1", team_id, pid, Position.PADDLER)
            self.svc.verify_membership("S1", m.mid, True)
            pids["women"].append(pid)
        return pids

    def _entry(self, team_id: str, category: Category, stage="ST4"):
        return self.svc.create_entry("S1", stage, team_id, category)

    def _checkin(self, entry, roster, n_men: int, n_women: int):
        ids = roster["men"][:n_men] + roster["women"][:n_women]
        return self.svc.freeze_checkin("S1", entry.eid, ids)

    # ---------- 注册与资格 ----------

    def test_unverified_member_cannot_check_in(self) -> None:
        pid = "TC-M1"
        self.svc.register_person("S1", pid, "待审队员", Gender.MALE)
        m = self.svc.add_membership("S1", "TC", pid, Position.PADDLER)
        # 未 verify
        entry = self._entry("TC", Category.MEN_OPEN)
        # 只放一人也会触发资格校验（在人数校验之前）
        with self.assertRaisesRegex(ValidationError, "资格未经秘书处确认"):
            self.svc.freeze_checkin("S1", entry.eid, [pid])

    def test_timer_cannot_register_team(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.svc.register_team("T1", "TX", "越权队")

    # ---------- 混合组比例 ----------

    def test_mixed_requires_eight_female_paddlers(self) -> None:
        entry = self._entry("TA", Category.MIXED)
        bad = self.mix_a["men"][:13] + self.mix_a["women"][:7]  # 20 人但只有 7 女
        with self.assertRaisesRegex(ValidationError, "女子不少于8人"):
            self.svc.freeze_checkin("S1", entry.eid, bad)

        good = self.mix_a["men"][:12] + self.mix_a["women"][:8]
        checkin = self.svc.freeze_checkin("S1", entry.eid, good)
        self.assertTrue(checkin.compliant)
        self.assertEqual(checkin.female_paddlers, MIXED_MIN_FEMALE_PADDLERS)

    def test_mixed_paddlers_must_be_exactly_twenty(self) -> None:
        entry = self._entry("TA", Category.MIXED)
        short = self.mix_a["men"][:12] + self.mix_a["women"][:7]
        with self.assertRaisesRegex(ValidationError, "划手人数应为20"):
            self.svc.freeze_checkin("S1", entry.eid, short)

    # ---------- 冻结与替补 ----------

    def test_roster_freezes_and_substitution_preserves_membership(self) -> None:
        entry = self._entry("TA", Category.MIXED)
        ids = self.mix_a["men"][:12] + self.mix_a["women"][:8]
        self.svc.freeze_checkin("S1", entry.eid, ids)

        # 冻结后再次冻结被拒绝
        with self.assertRaises(RosterFrozenError):
            self.svc.freeze_checkin("S1", entry.eid, ids)

        out = self.mix_a["women"][0]
        spare = self.mix_a["women"][8]
        sub = self.svc.request_substitution(
            "STA", entry.eid, out, spare, SubReason.INJURY, "训练中腰部拉伤"
        )
        # 未写影响说明不能批准
        with self.assertRaisesRegex(ValidationError, "必须说明积分"):
            self.svc.review_substitution("S1", sub.sid, True)

        approved = self.svc.review_substitution(
            "S1", sub.sid, True,
            points_affected=False, qualification_affected=False,
            impact_note="赛前因伤换人，人数与女子比例不变，积分与晋级不受影响",
        )
        self.assertEqual(approved.status.value, "已批准")

        # 新名单版本：替补上场，原成员关系保留为“已被替补”
        latest = self.svc.latest_checkin(entry.eid)
        self.assertEqual(latest.version, 2)
        self.assertIn(spare, [r.person_id for r in latest.rows])
        self.assertNotIn(out, [r.person_id for r in latest.rows])
        original = self.svc.store.memberships[approved.original_membership_id]
        self.assertEqual(original.status.value, "已被替补")
        self.assertIsNotNone(self.svc.store.memberships.get(original.mid))

    def test_mixed_substitution_with_male_spare_is_rejected(self) -> None:
        entry = self._entry("TA", Category.MIXED)
        ids = self.mix_a["men"][:12] + self.mix_a["women"][:8]
        self.svc.freeze_checkin("S1", entry.eid, ids)
        sub = self.svc.request_substitution(
            "STA", entry.eid, self.mix_a["women"][0], self.mix_a["men"][12],
            SubReason.ILLNESS,
        )
        with self.assertRaisesRegex(ValidationError, "名单不再合规"):
            self.svc.review_substitution(
                "S1", sub.sid, True, True, False, "尝试男子替补女划手"
            )
        self.assertEqual(self.svc.store.substitutions[sub.sid].status.value, "已驳回")

    def test_team_staff_cannot_substitute_other_team(self) -> None:
        entry = self._entry("TB", Category.MIXED)
        ids = self.mix_b["men"][:12] + self.mix_b["women"][:8]
        self.svc.freeze_checkin("S1", entry.eid, ids)
        with self.assertRaises(PermissionDeniedError):
            self.svc.request_substitution(
                "STA", entry.eid, self.mix_b["women"][0], self.mix_b["women"][8],
                SubReason.INJURY,
            )

    # ---------- 赛道/船只调整 ----------

    def test_lane_adjustment_requires_reason_and_impact(self) -> None:
        race = self.svc.arrange_race("R1", "RM1", "ST4", Category.MEN_OPEN, "预赛")
        self.svc.assign_lane("R1", "RM1", "TA", 1, "B1")
        self.svc.assign_lane("R1", "RM1", "TB", 2, "B2")
        with self.assertRaisesRegex(ValidationError, "理由与影响说明"):
            self.svc.adjust_lane("S1", "RM1", "TA", 3, None, "", False, False, "")
        self.svc.adjust_lane(
            "S1", "RM1", "TA", 3, None, "3号航道浮标移位",
            False, False, "仅更换航道，出发条件一致，不影响积分与晋级",
        )
        history = self.svc.store.lanes["RM1"]
        self.assertEqual(history[-1].lane, 3)
        actions = [e.action for e in self.svc.audit_log("S1")]
        self.assertIn("赛道船只调整", actions)

    def test_lane_cannot_collide(self) -> None:
        race = self.svc.arrange_race("R1", "RM2", "ST4", Category.MEN_OPEN, "预赛")
        self.svc.assign_lane("R1", "RM2", "TA", 1, "B1")
        self.svc.assign_lane("R1", "RM2", "TB", 2, "B2")
        with self.assertRaisesRegex(ValidationError, "赛道已被占用"):
            self.svc.adjust_lane(
                "S1", "RM2", "TA", 2, None, "测试", False, False, "撞到2号"
            )

    # ---------- 成绩幂等、冲突锁定 ----------

    def _arrange_mixed_race(self, rid="RX1"):
        race = self.svc.arrange_race("R1", rid, "ST4", Category.MIXED, "决赛")
        ea = self._entry("TA", Category.MIXED)
        eb = self._entry("TB", Category.MIXED)
        self._checkin(ea, self.mix_a, 12, 8)
        self._checkin(eb, self.mix_b, 12, 8)
        return race

    def test_duplicate_result_returns_original(self) -> None:
        self._arrange_mixed_race()
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        first, outcome1 = self.svc.submit_result("T1", "RX1", lines)
        self.assertEqual(outcome1, "received")
        again, outcome2 = self.svc.submit_result("T1", "RX1", list(lines))
        self.assertEqual(outcome2, "duplicate")
        self.assertIs(again, first)
        # 没有产生第二条接收记录
        self.assertEqual(len(self.svc.store.results["RX1"]), 1)

    def test_conflicting_result_locks_only_that_race(self) -> None:
        self._arrange_mixed_race()
        # 另一组别（男子公开组）场次，用于验证“其他组别照常结算”
        race_men = self.svc.arrange_race("R1", "RM9", "ST4", Category.MEN_OPEN, "决赛")
        ea = self._entry("TA", Category.MEN_OPEN)
        eb = self._entry("TB", Category.MEN_OPEN)
        self._checkin(ea, self.men_a, 20, 0)
        self._checkin(eb, self.men_b, 20, 0)

        v1 = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        first, _ = self.svc.submit_result("T1", "RX1", v1)
        # 异内容第二次接收 → 锁定
        v2 = [ResultLine("TB", 119800, 1), ResultLine("TA", 121100, 2)]
        second, outcome = self.svc.submit_result("R1", "RX1", v2, "裁判记录台复核")
        self.assertEqual(outcome, "conflict")
        self.assertEqual(self.svc.store.races["RX1"].status, RaceStatus.LOCKED)

        open_appeals = [
            a for a in self.svc.store.appeals.values() if a.status == AppealStatus.OPEN
        ]
        self.assertEqual(len(open_appeals), 1)
        self.assertEqual(open_appeals[0].candidate_rsids, [first.rsid, second.rsid])

        # 锁定场不能确认
        with self.assertRaises(PointsLockedError):
            self.svc.confirm_result("R2", "RX1")

        # 男子公开组照常提交并由另一位裁判确认结算
        self.svc.assign_lane("R1", "RM9", "TA", 1, "B1")
        self.svc.assign_lane("R1", "RM9", "TB", 2, "B2")
        men_lines = [ResultLine("TA", 115000, 1), ResultLine("TB", 117000, 2)]
        self.svc.submit_result("T1", "RM9", men_lines)
        ledger = self.svc.confirm_result("R2", "RM9")
        self.assertEqual(self.svc.store.races["RM9"].status, RaceStatus.SETTLED)
        self.assertEqual({e.team_id: e.points for e in ledger}, {"TA": 100, "TB": 85})

    def test_conflict_after_settlement_freezes_existing_points(self) -> None:
        self._arrange_mixed_race("RX2")
        v1 = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RX2", v1)
        self.svc.confirm_result("R2", "RX2")
        # 同内容重复：幂等，不改动
        _, outcome = self.svc.submit_result("T1", "RX2", list(v1))
        self.assertEqual(outcome, "duplicate")
        self.assertEqual(self.svc.store.races["RX2"].status, RaceStatus.SETTLED)
        # 异内容：已结算积分转为冻结
        v2 = [ResultLine("TB", 119800, 1), ResultLine("TA", 121100, 2)]
        self.svc.submit_result("R1", "RX2", v2)
        self.assertTrue(
            all(e.status == LedgerStatus.LOCKED for e in self.svc.current_ledger("RX2"))
        )

    def test_submitter_cannot_confirm_own_result(self) -> None:
        self._arrange_mixed_race("RX3")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("R1", "RX3", lines)
        with self.assertRaisesRegex(PermissionDeniedError, "不能独自确认"):
            self.svc.confirm_result("R1", "RX3")
        # 另一位裁判可以确认
        ledger = self.svc.confirm_result("R2", "RX3")
        self.assertEqual(len(ledger), 2)

    def test_timer_cannot_confirm_result(self) -> None:
        self._arrange_mixed_race("RX4")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RX4", lines)
        with self.assertRaises(PermissionDeniedError):
            self.svc.confirm_result("T1", "RX4")

    # ---------- 成绩更正版本 ----------

    def test_result_correction_creates_new_ledger_version(self) -> None:
        self._arrange_mixed_race("RX5")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RX5", lines)
        self.svc.confirm_result("R2", "RX5")
        corrected = [ResultLine("TB", 119800, 1), ResultLine("TA", 121100, 2)]
        submission, ledger = self.svc.correct_result(
            "S1", "RX5", corrected, "计时传感器漏帧，终点录像复核",
            points_affected=True, qualification_affected=False,
        )
        self.assertEqual(submission.note.startswith("更正："), True)
        self.assertEqual({e.team_id: e.points for e in ledger}, {"TB": 100, "TA": 85})
        self.assertTrue(all(e.result_version == 2 for e in ledger))
        # 旧台账保留但作废，可审计
        old = [e for e in self.svc.store.ledger["RX5"] if e.result_version == 1]
        self.assertTrue(all(e.status == LedgerStatus.VOIDED for e in old))
        actions = [e.action for e in self.svc.audit_log("S1")]
        self.assertIn("成绩更正", actions)

    # ---------- 申诉双人复核 ----------

    def test_appeal_resolver_must_not_be_submitter(self) -> None:
        self._arrange_mixed_race("RX6")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        first, _ = self.svc.submit_result("R1", "RX6", lines)
        appeal = self.svc.raise_appeal("STB", "RX6", "终点判罚存疑")
        self.assertEqual(appeal.status, AppealStatus.OPEN)
        # R1 是提交人，不能参与裁决（即使他是裁判，也无权 resolve；
        # 这里再验证秘书处若恰好提交过也被规则拦截）
        with self.assertRaises(PermissionDeniedError):
            self.svc.resolve_appeal(
                "R1", appeal.aid, False, None, "越权", False, False
            )
        # 秘书处 S1 未提交成绩，可以裁决；申诉成立选第二版本前需先有第二版本
        v2 = [ResultLine("TB", 119800, 1), ResultLine("TA", 121100, 2)]
        second, _ = self.svc.submit_result("T1", "RX6", v2)
        # 秘书处 S1 未提交成绩，可以裁决并选定计时员版本
        appeal = self.svc.resolve_appeal(
            "S1", appeal.aid, True, second.rsid, "终点录像支持计时员版本",
            points_affected=True, qualification_affected=True,
        )
        self.assertEqual(appeal.status, AppealStatus.UPHELD)
        current = {e.team_id: e.points for e in self.svc.current_ledger("RX6")}
        self.assertEqual(current, {"TB": 100, "TA": 85})
        self.assertEqual(self.svc.store.races["RX6"].status, RaceStatus.SETTLED)

    def test_secretariat_submitter_must_also_recuse_from_appeal(self) -> None:
        self.svc.register_user("S1", "S2", "秘书处乙", Role.SECRETARIAT)
        self.svc.register_user("S1", "S3", "秘书处丙", Role.SECRETARIAT)
        self._arrange_mixed_race("RXA")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RXA", lines)
        self.svc.confirm_result("R2", "RXA")
        # S2 主动更正成绩，成为该场成绩版本提交人
        corrected, _ = self.svc.correct_result(
            "S2", "RXA",
            [ResultLine("TB", 119800, 1), ResultLine("TA", 121100, 2)],
            "录像复核名次判反", True, False,
        )
        # 再来一份异内容成绩触发锁定
        self.svc.submit_result(
            "R1", "RXA", [ResultLine("TA", 120000, 1), ResultLine("TB", 119020, 2)]
        )
        appeal = next(
            a for a in self.svc.store.appeals.values() if a.status == AppealStatus.OPEN
        )
        with self.assertRaisesRegex(PermissionDeniedError, "不能独自确认争议"):
            self.svc.resolve_appeal(
                "S2", appeal.aid, True, corrected.rsid, "更正人自裁", True, False
            )
        # 未参与提交的 S3 可以裁决
        self.svc.resolve_appeal(
            "S3", appeal.aid, True, corrected.rsid, "独立复核维持更正版本", True, False
        )
        self.assertEqual(self.svc.store.races["RXA"].status, RaceStatus.SETTLED)

    def test_rejected_appeal_restores_confirmed_points(self) -> None:
        self._arrange_mixed_race("RX7")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RX7", lines)
        self.svc.confirm_result("R2", "RX7")
        appeal = self.svc.raise_appeal("STB", "RX7", "要求复核")
        self.assertTrue(
            all(e.status == LedgerStatus.LOCKED for e in self.svc.current_ledger("RX7"))
        )
        self.svc.resolve_appeal(
            "S1", appeal.aid, False, None, "录像与原成绩一致，维持原判",
            points_affected=False, qualification_affected=False,
        )
        self.assertTrue(
            all(e.status == LedgerStatus.CONFIRMED for e in self.svc.current_ledger("RX7"))
        )

    # ---------- 可见范围与晋级解释 ----------

    def test_team_visibility_and_qualification_explanation(self) -> None:
        self._arrange_mixed_race("RX8")
        lines = [ResultLine("TA", 120000, 1), ResultLine("TB", 122500, 2)]
        self.svc.submit_result("T1", "RX8", lines)
        self.svc.confirm_result("R2", "RX8")
        # 换人留痕
        sub = self.svc.request_substitution(
            "STA",
            next(iter({e.eid for e in self.svc.store.entries.values()
                       if e.team_id == "TA" and e.category == Category.MIXED})),
            self.mix_a["women"][1], self.mix_a["women"][8],
            SubReason.EMERGENCY, "家中急事",
        )
        self.svc.review_substitution(
            "S1", sub.sid, True, False, False, "同位置同性别替补，不影响积分与晋级"
        )

        q = QualificationQuery(self.svc)
        # 队伍工作人员不能看别队
        with self.assertRaises(PermissionDeniedError):
            q.team_view("STA", "TB", Category.MIXED)
        # 计时员无晋级查询权限
        with self.assertRaises(PermissionDeniedError):
            q.team_view("T1", "TA", Category.MIXED)

        view = q.team_view("STA", "TA", Category.MIXED)
        self.assertEqual(view.confirmed_points, 100)
        self.assertTrue(any(s.sid == sub.sid for s in view.substitutions))
        text = q.explain("STA", "TA", Category.MIXED)
        self.assertIn("积分来源", text)
        self.assertIn("换人记录", text)
        self.assertIn("未决申诉", text)

        # 出现未决申诉后：队伍侧看不到冻结分值
        self.svc.raise_appeal("STB", "RX8", "争议")
        view_locked = q.team_view("STA", "TA", Category.MIXED)
        self.assertEqual(view_locked.confirmed_points, 0)
        self.assertEqual(len(view_locked.open_appeals), 1)
        masked = [p for p in view_locked.point_sources if p.status == LedgerStatus.LOCKED]
        self.assertTrue(masked)
        self.assertTrue(all(p.points == 0 for p in masked))

    def test_final_groups_rank_by_confirmed_points(self) -> None:
        # 男子公开组：TA 两胜、TB 一胜一亚、TC 三亚
        self._build_roster("TC", men=20, women=0)
        rosters = {
            "TA": self.men_a,
            "TB": self.men_b,
            "TC": {"men": [f"TC-M{i}" for i in range(1, 21)], "women": []},
        }
        for t in ("TA", "TB", "TC"):
            entry = self._entry(t, Category.MEN_OPEN)
            self._checkin(entry, rosters[t], 20, 0)
        for rid, winner, runner in (("RG1", "TA", "TB"), ("RG2", "TA", "TC"),
                                    ("RG3", "TB", "TC")):
            race = self.svc.arrange_race("R1", rid, "ST4", Category.MEN_OPEN, "决赛")
            self.svc.submit_result(
                "T1", race.rid,
                [ResultLine(winner, 120000, 1), ResultLine(runner, 122000, 2)],
            )
            self.svc.confirm_result("R2", race.rid)

        q = QualificationQuery(self.svc)
        groups = q.final_groups("S1", Category.MEN_OPEN)
        champion = groups["冠军组"]
        self.assertEqual([g.team_id for g in champion], ["TA", "TB", "TC"])
        self.assertEqual(champion[0].confirmed_points, 200)
        # 每一行分组结果都带完整解释
        self.assertIn("积分来源", q.explain("S1", "TA", Category.MEN_OPEN))


if __name__ == "__main__":
    unittest.main()
