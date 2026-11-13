"""质量监控业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.quality_registry import (
    ALERT_LEVELS,
    DEFAULT_ALERT_LEVEL,
    DEFAULT_PRIORITY,
    DEFAULT_RECOVERY,
    HISTORY_DAYS,
    quality_registry,
)
from app.store import store

MODULE = "quality"
REQUIRED_FIELDS = ["指标编号", "监控对象", "指标类型"]
OPTIONAL_FIELDS = ["阈值范围", "检测周期", "告警级别", "通知渠道", "恢复策略", "业务优先级"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用监控": "已启用", "停用监控": "已停用"}
NEGATIVE_ACTIONS = ["停用监控"]
DEFAULT_THRESHOLD = "0.95"


class QualityService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        object_name: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if object_name:
            rows = [row for row in rows if str(row.get("监控对象", "")) == object_name]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        # 阈值版本、生效阈值与当前告警级别统一由登记簿补齐，列表和告警通知读同一份。
        return [quality_registry.decorate_metric(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return quality_registry.decorate_metric(entry) if entry else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str | None]:
        """登记指标：返回 (明细, 缺失字段, 错误说明)；错误说明非空时没有落库。"""
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, None
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("阈值范围", DEFAULT_THRESHOLD)
        entry.setdefault("检测周期", "每日")
        entry.setdefault("通知渠道", "企业微信")
        entry.setdefault("告警级别", DEFAULT_ALERT_LEVEL)
        entry.setdefault("恢复策略", DEFAULT_RECOVERY)
        entry.setdefault("业务优先级", DEFAULT_PRIORITY)

        version, error = quality_registry.register_metric(entry)
        if error:
            # 阈值不合法时不落库，避免把超范围阈值保存进指标列表。
            return entry, [], error
        rows.append(entry)
        return quality_registry.decorate_metric(entry), [], None

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量指标 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于质量监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return quality_registry.decorate_metric(entry), f"质量指标已{action}"

    def change_threshold(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None, bool, str | None]:
        """调整阈值：返回 (明细, 说明, 版本, 是否重复提交, 错误说明)。"""
        metric, message, version, duplicate = quality_registry.change_threshold(entry_id, values)
        if metric is None:
            return None, message, None, duplicate, message
        if version is None:
            return quality_registry.decorate_metric(metric), message, None, duplicate, message
        return quality_registry.decorate_metric(metric), message, version, duplicate, None

    def list_versions(self, *, metric_id: int | None = None, object_name: str | None = None) -> list[dict[str, Any]]:
        return quality_registry.list_versions(metric_id=metric_id, object_name=object_name)

    def list_results(
        self,
        *,
        metric_id: int | None = None,
        object_name: str | None = None,
        days: int = HISTORY_DAYS,
    ) -> list[dict[str, Any]]:
        return quality_registry.list_detections(metric_id=metric_id, object_name=object_name, days=days)

    def list_alerts(self, *, metric_id: int | None = None, object_name: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return quality_registry.list_alerts(metric_id=metric_id, object_name=object_name, status=status)

    def resolve_conflicts(self, *, object_name: str | None = None) -> list[dict[str, Any]]:
        return quality_registry.resolve_conflicts(object_name=object_name)

    def list_recovery(self) -> list[dict[str, Any]]:
        return quality_registry.list_recovery()

    def version_sync(self) -> list[dict[str, Any]]:
        return quality_registry.version_sync()

    def alert_levels(self) -> list[str]:
        return list(ALERT_LEVELS)
