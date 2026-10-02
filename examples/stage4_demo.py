"""第四站全流程演示（示例数据，不含真实身份信息）。

运行：
    python3 -m examples.stage4_demo

覆盖：注册与资格 → 报名 → 检录冻结（混合组 8 女划手）→ 伤病替补 →
赛道调整 → 成绩提交与双人确认 → 重复成绩幂等/异内容锁定（其他组别照常结算）→
申诉复核 → 成绩更正版本 → 晋级解释。
"""

from __future__ import annotations

from src.dragon import (
    AppealStatus,
    Category,
    Gender,
    LeagueService,
    PermissionDeniedError,
    Position,
    ResultLine,
    Role,
    SubReason,
)
from src.dragon.qualification import QualificationQuery


def main() -> None:
    svc = LeagueService()
    line = lambda *a: print(*a)  # noqa: E731

    # 1. 用户与角色 ----------------------------------------------------------
    svc.register_user("__bootstrap__", "S1", "秘书处·林秘书", Role.SECRETARIAT)
    svc.register_user("S1", "S2", "秘书处·吴秘书长", Role.SECRETARIAT)
    svc.register_user("S1", "R1", "裁判·陈裁判长", Role.REFEREE)
    svc.register_user("S1", "R2", "裁判·黄副裁判长", Role.REFEREE)
    svc.register_user("S1", "T1", "计时员·周计时", Role.TIMER)

    for tid, name in (("TA", "珠江猛龙队"), ("TB", "花城凤凰队"), ("TC", "荔湾蛟龙队")):
        svc.register_team("S1", tid, name)
        svc.register_user("S1", f"ST-{tid}", f"{name}工作人员", Role.TEAM_STAFF, team_id=tid)

    def build_roster(tid: str, men: int, women: int) -> tuple[list[str], list[str]]:
        male_ids, female_ids = [], []
        for i in range(men):
            pid = f"{tid}-M{i+1:02d}"
            svc.register_person("S1", pid, f"男划手{i+1:02d}", Gender.MALE)
            m = svc.add_membership("S1", tid, pid, Position.PADDLER)
            svc.verify_membership("S1", m.mid, True)
            male_ids.append(pid)
        for i in range(women):
            pid = f"{tid}-F{i+1:02d}"
            svc.register_person("S1", pid, f"女划手{i+1:02d}", Gender.FEMALE)
            m = svc.add_membership("S1", tid, pid, Position.PADDLER)
            svc.verify_membership("S1", m.mid, True)
            female_ids.append(pid)
        return male_ids, female_ids

    roster = {tid: build_roster(tid, 21, 9) for tid in ("TA", "TB", "TC")}

    for i in range(1, 5):
        svc.create_stage(f"ST{i}", f"广州龙舟超级联赛第{i}站", i)
    svc.register_boat("R1", "B1", "标准龙舟01")
    svc.register_boat("R1", "B2", "标准龙舟02")
    svc.register_boat("R1", "B3", "标准龙舟03")

    # 2. 第四站报名 ----------------------------------------------------------
    entries: dict[tuple[str, Category], str] = {}
    for tid in ("TA", "TB", "TC"):
        e = svc.create_entry("S1", "ST4", tid, Category.MEN_OPEN)
        entries[(tid, Category.MEN_OPEN)] = e.eid
    for tid in ("TA", "TB"):
        e = svc.create_entry("S1", "ST4", tid, Category.MIXED)
        entries[(tid, Category.MIXED)] = e.eid
    line("== 第四站报名完成：男子公开组 3 队，男女混合组 2 队 ==")

    # 3. 检录冻结 ------------------------------------------------------------
    for tid in ("TA", "TB", "TC"):
        men, women = roster[tid]
        svc.freeze_checkin("S1", entries[(tid, Category.MEN_OPEN)], men[:20])
    for tid in ("TA", "TB"):
        men, women = roster[tid]
        checkin = svc.freeze_checkin(
            "S1", entries[(tid, Category.MIXED)], men[:12] + women[:8]
        )
        line(f"{tid} 混合组检录冻结：{checkin.paddler_count}名划手，"
             f"其中女划手{checkin.female_paddlers}名，合规={checkin.compliant}")

    # 4. 赛前伤病替补（猛龙队女划手 F01 受伤，F09 替换） ----------------------
    men_a, women_a = roster["TA"]
    sub = svc.request_substitution(
        "ST-TA", entries[("TA", Category.MIXED)],
        women_a[0], women_a[8], SubReason.INJURY, "赛前热身肩部拉伤，有医务证明",
    )
    svc.review_substitution(
        "S1", sub.sid, True,
        points_affected=False, qualification_affected=False,
        impact_note="赛前同性别同位置替补，20名划手且女子仍为8人以上，积分与总决赛晋级不受影响",
    )
    line(f"== 替补 {sub.sid} 已批准：{women_a[0]}→{women_a[8]}，"
         f"积分影响=否，晋级影响=否；原成员关系保留为“已被替补” ==")

    # 5. 赛道调整 ------------------------------------------------------------
    svc.arrange_race("R1", "R-MIX-F", "ST4", Category.MIXED, "决赛")
    svc.assign_lane("R1", "R-MIX-F", "TA", 1, "B1")
    svc.assign_lane("R1", "R-MIX-F", "TB", 2, "B2")
    svc.adjust_lane(
        "S1", "R-MIX-F", "TA", 3, "B3", "1号航道起点浮标松动，改用3号航道",
        False, False, "航道条件经裁判长核验一致，仅换道换船，积分与晋级不受影响",
    )
    line("== TA 由1号赛道/01船调整至3号赛道/03船，影响说明已留痕 ==")

    # 6. 混合组决赛：计时员提交，另一裁判确认 --------------------------------
    official_lines = [ResultLine("TA", 118640, 1), ResultLine("TB", 120120, 2)]
    first, outcome = svc.submit_result("T1", "R-MIX-F", official_lines, "终点光电计时")
    line(f"== 首次接收成绩 {first.rsid}：{outcome} ==")
    again, outcome = svc.submit_result("T1", "R-MIX-F", list(official_lines))
    line(f"== 同内容重复接收：{outcome}，返回原记录 {again.rsid}（未重复入账）==")
    ledger = svc.confirm_result("R2", "R-MIX-F")  # R2 非提交人
    line(f"== 裁判R2确认结算：" +
        "，".join(f"{e.team_id} {e.points}分(第{e.place}名)" for e in ledger) + " ==")

    # 7. 秘书处赛后成绩更正：录像复核发现名次判反，积分真实对调，生成 v2 台账
    corrected = [ResultLine("TB", 118350, 1), ResultLine("TA", 119020, 2)]
    correction_rs, v2_ledger = svc.correct_result(
        "S1", "R-MIX-F", corrected,
        "终点录像复核：TB 队先于 TA 0.67 秒压线，光电计时终点帧对调，秘书处核定更正",
        points_affected=True, qualification_affected=False,
    )
    line("== 秘书处成绩更正，台账 v2：" +
        "，".join(f"{e.team_id}={e.points}分[v{e.result_version}]" for e in v2_ledger) +
        "；单站积分对调已说明，四站累计后的总决赛分组结论暂不变 ==")

    # 8. 更正后又收到一份与各版本都不一致的手工抄表 → 锁定该场
    #    男子公开组另一场不受影响，照常结算
    handwritten = [ResultLine("TA", 118640, 1), ResultLine("TB", 119020, 2)]
    _, outcome = svc.submit_result("R1", "R-MIX-F", handwritten, "记录台手工秒表补抄")
    line(f"== 又一份异内容成绩：{outcome}，R-MIX-F 积分再次锁定，自动立案争议；"
         f"候选版本 {[r.rsid for r in svc.store.results['R-MIX-F']]} ==")
    locked = svc.current_ledger("R-MIX-F")
    line("   锁定台账：" + "，".join(f"{e.team_id}={e.points}分[{e.status.value}]" for e in locked))

    svc.arrange_race("R1", "R-MEN-F", "ST4", Category.MEN_OPEN, "决赛")
    for idx, tid in enumerate(("TA", "TB", "TC"), start=1):
        svc.assign_lane("R1", "R-MEN-F", tid, idx, f"B{idx}")
    men_lines = [
        ResultLine("TA", 112300, 1), ResultLine("TC", 113880, 2), ResultLine("TB", 115220, 3)
    ]
    svc.submit_result("T1", "R-MEN-F", men_lines)
    men_ledger = svc.confirm_result("R2", "R-MEN-F")
    line("== 男子公开组决赛不受混合组争议影响，照常结算：" +
        "，".join(f"{e.team_id} {e.points}分" for e in men_ledger) + " ==")

    # 9. 双人复核争议：裁判无权裁决；秘书处更正者 S1 是候选版本提交人，必须回避
    appeal = next(a for a in svc.store.appeals.values() if a.status == AppealStatus.OPEN)
    try:
        svc.resolve_appeal("R1", appeal.aid, False, None, "裁判试图自裁", False, False)
    except PermissionDeniedError as exc:
        line(f"== 规则拦截（角色层）：{exc} ==")
    try:
        svc.resolve_appeal("S1", appeal.aid, True, correction_rs.rsid, "更正人试图自裁", True, False)
    except PermissionDeniedError as exc:
        line(f"== 规则拦截（双人复核层）：{exc} ==")
    # 未提交过任何候选成绩的另一位秘书处成员裁决
    svc.resolve_appeal(
        "S2", appeal.aid, True, correction_rs.rsid,
        "复看终点录像与航道机位，确认秘书处核定版本（TB 第一）成立；手工秒表补抄作废",
        points_affected=True, qualification_affected=False,
    )
    restored = svc.current_ledger("R-MIX-F")
    line("== 吴秘书长裁决：采用秘书处核定版本并重算解锁：" +
        "，".join(f"{e.team_id}={e.points}分[v{e.result_version}]" for e in restored) + " ==")

    # 10. 晋级解释 -----------------------------------------------------------
    q = QualificationQuery(svc)
    print()
    print(q.explain("S1", "TA", Category.MIXED))
    print()
    print("== 角色可见范围 ==")
    print(f"秘书处：{svc.visible_scope('S1')}")
    print(f"裁判：{svc.visible_scope('R1')}")
    print(f"计时员：{svc.visible_scope('T1')}")
    print(f"队伍工作人员：{svc.visible_scope('ST-TA')}")
    print()
    print("== 秘书处留痕（换人/赛道/成绩争议/更正的积分与晋级影响说明） ==")
    for evt in svc.audit_log("S1"):
        print(f"[{evt.at}] {evt.action}｜{evt.target}｜积分影响={evt.points_affected}"
              f"｜晋级影响={evt.qualification_affected}｜{evt.detail}")


if __name__ == "__main__":
    main()
