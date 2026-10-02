"""赛事后台的领域异常。"""

from __future__ import annotations


class DomainError(ValueError):
    """所有业务规则错误的基类。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class ValidationError(DomainError):
    """提交内容不满足结构或资格规则。"""


class PermissionDeniedError(DomainError):
    """当前角色无权执行该操作或查看该范围。"""


class RosterFrozenError(DomainError):
    """检录名单已冻结，必须走替补流程。"""


class PointsLockedError(DomainError):
    """该场积分因成绩冲突或申诉被锁定。"""
