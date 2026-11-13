"""质量阈值版本化登记簿。

这里是质量监控里「阈值版本 / 检测结果 / 告警通知 / 冲突裁决」的唯一数据源：
- 每次阈值调整生成一个版本，重复提交同一修改复用已有版本号；
- 活跃指标按新口径生成新批次，历史检测结果保留当时阈值与当时告警级别；
- 告警通知保留触发时的阈值快照，恢复策略则读取当前生效版本；
- 同一监控对象的多个指标冲突时，先按业务优先级、再按告警级别裁决；
- 阈值变更同步写入聚合视图与治理规则，两个入口读取同一份告警级别。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

QUALITY_MODULE = "quality"
HISTORY_DAYS = 7
THRESHOLD_MIN = 0.0
THRESHOLD_MAX = 1.0
ALERT_LEVELS = ["提示", "一般", "严重"]
ALERT_LEVEL_RANK = {"提示": 1, "低": 1, "一般": 2, "警告": 2, "中": 2, "严重": 3, "高": 3, "紧急": 4}
PRIORITY_RANK = {"P0": 4, "高": 4, "P1": 3, "中": 3, "P2": 2, "低": 2, "P3": 1}
DEFAULT_ALERT_LEVEL = "一般"
DEFAULT_PRIORITY = "P2"
DEFAULT_RECOVERY = "连续 3 个检测批次达到阈值自动恢复"

# 示例检测值：前 6 天为历史口径，最后一天是调整前的当日批次。
_SEED_VALUES: dict[int, list[float]] = {
    1: [0.988, 0.981, 0.992, 0.979, 0.984, 0.995, 0.992],
    2: [0.972, 0.968, 0.981, 0.965, 0.988, 0.984, 0.953],
    3: [0.952, 0.946, 0.958, 0.961, 0.955, 0.949, 0.942],
    4: [0.956, 0.951, 0.947, 0.938, 0.952, 0.941, 0.949],
}


def format_threshold(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_threshold(raw: Any) -> tuple[tuple[float, float] | None, str | None]:
    """解析阈值：支持单值或 a~b 区间，只接受 0~1 之间的数值。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None, "阈值为空，请填写 0~1 之间的数值或区间"
    normalized = text.replace("～", "~").replace("至", "~")
    if "~" in normalized:
        left, _, right = normalized.partition("~")
        try:
            low, high = float(left.strip()), float(right.strip())
        except ValueError:
            return None, f"阈值区间「{text}」无法解析，请使用 a~b 形式"
        if not (THRESHOLD_MIN <= low <= THRESHOLD_MAX and THRESHOLD_MIN <= high <= THRESHOLD_MAX):
            return None, (
                f"阈值区间「{text}」超出允许范围 "
                f"{format_threshold(THRESHOLD_MIN)}~{format_threshold(THRESHOLD_MAX)}，未保存"
            )
        if low > high:
            return None, f"阈值区间「{text}」下界大于上界，未保存"
        return (low, high), None
    try:
        value = float(normalized)
    except ValueError:
        return None, f"阈值「{text}」无法解析为数值，未保存"
    if not (THRESHOLD_MIN <= value <= THRESHOLD_MAX):
        return None, (
            f"阈值「{text}」超出允许范围 "
            f"{format_threshold(THRESHOLD_MIN)}~{format_threshold(THRESHOLD_MAX)}，未保存"
        )
    return (value, value), None


def threshold_display(bounds: tuple[float, float]) -> str:
    low, high = bounds
    if low == high:
        return format_threshold(low)
    return f"{format_threshold(low)}~{format_threshold(high)}"


def judge_value(value: float, bounds: tuple[float, float]) -> bool:
    """单值阈值按「不低于阈值」判定，区间阈值按闭区间判定。"""
    low, high = bounds
    if low == high:
        return value >= low
    return low <= value <= high


def _priority_rank(value: Any) -> int:
    return PRIORITY_RANK.get(str(value or "").strip(), 0)


def _alert_rank(value: Any) -> int:
    return ALERT_LEVEL_RANK.get(str(value or "").strip(), 0)


# 上线时已经发生的阈值调整：按「0.98 → 0.995」演示口径迁移，过去七天结果保留。
_BOOTSTRAP_ADJUSTMENTS: dict[str, dict[str, Any]] = {
    "QUAL-0001": {"阈值范围": "0.995", "备注": "按业务要求将销售额质量阈值由 0.98 调整为 0.995"},
}


class QualityRegistry:
    """阈值版本、检测批次、告警快照与冲突裁决的内存登记簿。"""

    def __init__(self) -> None:
        self.versions: list[dict[str, Any]] = []
        self.detections: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self._fingerprints: dict[str, dict[str, Any]] = {}
        self._version_seq = 0
        self._detection_seq = 0
        self._alert_seq = 0
        self._batch_seq: dict[str, int] = {}
        self._seeded = False
        self.ensure_seeded()

    # ------------------------------------------------------------------ 基础数据
    def _metrics(self) -> list[dict[str, Any]]:
        return store.rows(QUALITY_MODULE)

    def _normalize_metric(self, metric: dict[str, Any]) -> None:
        metric.setdefault("业务优先级", DEFAULT_PRIORITY)
        metric.setdefault("告警级别", DEFAULT_ALERT_LEVEL)
        metric.setdefault("恢复策略", DEFAULT_RECOVERY)
        if str(metric.get("告警级别") or "") not in ALERT_LEVELS:
            metric["告警级别"] = DEFAULT_ALERT_LEVEL
        if not str(metric.get("业务优先级") or "").strip():
            metric["业务优先级"] = DEFAULT_PRIORITY

    def metric_bounds(self, metric: dict[str, Any]) -> tuple[float, float]:
        bounds, _ = parse_threshold(metric.get("阈值范围"))
        return bounds or (THRESHOLD_MIN, THRESHOLD_MAX)

    def current_version(self, metric: dict[str, Any]) -> dict[str, Any] | None:
        for version in self.versions:
            if version["指标编号"] == metric.get("指标编号") and version["状态"] == "生效":
                return version
        return None

    def current_alert_level(self, metric: dict[str, Any]) -> str:
        """告警级别的单一读取入口：列表、告警通知与冲突裁决都读这里。"""
        return str(metric.get("告警级别") or DEFAULT_ALERT_LEVEL)

    # ------------------------------------------------------------------ 版本
    def _create_version(
        self,
        metric: dict[str, Any],
        bounds: tuple[float, float],
        alert_level: str,
        priority: str,
        *,
        previous: dict[str, Any] | None = None,
        fingerprint: str = "",
        change_type: str = "初始化",
        note: str = "",
    ) -> dict[str, Any]:
        self._version_seq += 1
        version = {
            "版本号": f"QV-{self._version_seq:04d}",
            "指标编号": metric.get("指标编号"),
            "监控对象": metric.get("监控对象"),
            "指标类型": metric.get("指标类型"),
            "阈值范围": threshold_display(bounds),
            "告警级别": alert_level,
            "业务优先级": priority,
            "原阈值": previous["阈值范围"] if previous else "",
            "原版本": previous["版本号"] if previous else "",
            "变更类型": change_type,
            "状态": "生效",
            "变更时间": datetime.now().isoformat(timespec="seconds"),
            "请求指纹": fingerprint,
            "备注": note,
        }
        if previous is not None:
            previous["状态"] = "历史"
        self.versions.append(version)
        return version

    def _fingerprint(
        self,
        metric: dict[str, Any],
        old_bounds: tuple[float, float],
        new_bounds: tuple[float, float],
        alert_level: str,
        priority: str,
    ) -> str:
        payload = {
            "指标编号": metric.get("指标编号"),
            "监控对象": metric.get("监控对象"),
            "指标类型": metric.get("指标类型"),
            "原阈值": threshold_display(old_bounds),
            "新阈值": threshold_display(new_bounds),
            "告警级别": alert_level,
            "业务优先级": priority,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def list_versions(self, *, metric_id: int | None = None, object_name: str | None = None) -> list[dict[str, Any]]:
        rows = self.versions
        if metric_id is not None:
            metric = store.find(QUALITY_MODULE, metric_id)
            code = metric.get("指标编号") if metric else None
            rows = [row for row in rows if row["指标编号"] == code]
        if object_name:
            rows = [row for row in rows if row["监控对象"] == object_name]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 检测与告警
    def _append_detection(
        self,
        metric: dict[str, Any],
        bounds: tuple[float, float],
        version_code: str,
        value: float,
        day: date,
        *,
        batch_kind: str,
        alert_level: str | None = None,
    ) -> dict[str, Any]:
        self._detection_seq += 1
        day_key = day.isoformat()
        batch_index = self._batch_seq.get(day_key, 0)
        self._batch_seq[day_key] = batch_index + 1
        passed = judge_value(value, bounds)
        detection = {
            "id": self._detection_seq,
            "批次序号": self._detection_seq,
            "批次号": f"BATCH-{day:%Y%m%d}-{batch_index}",
            "批次类型": batch_kind,
            "指标编号": metric.get("指标编号"),
            "监控对象": metric.get("监控对象"),
            "指标类型": metric.get("指标类型"),
            "检测日期": day_key,
            "实测值": round(float(value), 6),
            "阈值": threshold_display(bounds),
            "阈值版本": version_code,
            "判定": "达标" if passed else "异常",
            "是否告警": not passed,
            "告警级别": alert_level or self.current_alert_level(metric),
            "判定口径": f"阈值 {threshold_display(bounds)}（版本 {version_code}）",
        }
        self.detections.append(detection)
        if not passed:
            self._create_alert(metric, detection, bounds, version_code)
        return detection

    def _create_alert(
        self,
        metric: dict[str, Any],
        detection: dict[str, Any],
        bounds: tuple[float, float],
        version_code: str,
    ) -> dict[str, Any]:
        self._alert_seq += 1
        alert = {
            "id": self._alert_seq,
            "告警编号": f"ALRT-{self._alert_seq:04d}",
            "指标编号": metric.get("指标编号"),
            "监控对象": metric.get("监控对象"),
            "指标类型": metric.get("指标类型"),
            "批次号": detection["批次号"],
            "发生时间": f'{detection["检测日期"]} {datetime.now().strftime("%H:%M:%S")}',
            "实测值": detection["实测值"],
            # 触发阈值与告警级别是快照：阈值再调整也不会改写已发生告警。
            "触发阈值": detection["阈值"],
            "阈值版本": version_code,
            "告警级别": detection["告警级别"],
            "状态": "处理中",
            "通知渠道": metric.get("通知渠道") or "企业微信",
            "恢复策略": metric.get("恢复策略") or DEFAULT_RECOVERY,
            # 恢复阈值读取当前生效版本，阈值调整后会刷新。
            "恢复阈值": detection["阈值"],
            "恢复阈值版本": version_code,
        }
        self.alerts.append(alert)
        return alert

    def refresh_recovery(self, metric: dict[str, Any], bounds: tuple[float, float], version_code: str) -> None:
        """活跃告警的恢复策略按新版本重算；已发生告警的触发阈值快照保持不变。"""
        for alert in self.alerts:
            if alert["指标编号"] != metric.get("指标编号") or alert["状态"] != "处理中":
                continue
            alert["恢复阈值"] = threshold_display(bounds)
            alert["恢复阈值版本"] = version_code
            alert["恢复策略"] = metric.get("恢复策略") or DEFAULT_RECOVERY

    def _latest_detection(self, metric: dict[str, Any]) -> dict[str, Any] | None:
        rows = [row for row in self.detections if row["指标编号"] == metric.get("指标编号")]
        if not rows:
            return None
        rows.sort(key=lambda row: (row["检测日期"], row["批次序号"]))
        return rows[-1]

    def list_detections(
        self,
        *,
        metric_id: int | None = None,
        object_name: str | None = None,
        days: int = HISTORY_DAYS,
    ) -> list[dict[str, Any]]:
        days = max(1, min(int(days), 365))
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = [row for row in self.detections if row["检测日期"] >= cutoff]
        if metric_id is not None:
            metric = store.find(QUALITY_MODULE, metric_id)
            code = metric.get("指标编号") if metric else None
            rows = [row for row in rows if row["指标编号"] == code]
        if object_name:
            rows = [row for row in rows if row["监控对象"] == object_name]
        current_codes = {
            metric["指标编号"]: (self.current_version(metric) or {}).get("版本号")
            for metric in self._metrics()
        }
        rows.sort(key=lambda row: (row["检测日期"], row["批次序号"]))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["当前阈值版本"] = current_codes.get(row["指标编号"], "")
            # 口径未更新的批次就是历史口径：保留当时阈值和当时告警级别。
            item["历史口径"] = row["阈值版本"] != item["当前阈值版本"]
            result.append(item)
        return result

    def list_alerts(
        self,
        *,
        metric_id: int | None = None,
        object_name: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.alerts)
        if metric_id is not None:
            metric = store.find(QUALITY_MODULE, metric_id)
            code = metric.get("指标编号") if metric else None
            rows = [row for row in rows if row["指标编号"] == code]
        if object_name:
            rows = [row for row in rows if row["监控对象"] == object_name]
        if status:
            rows = [row for row in rows if row["状态"] == status]
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metric = self._metric_by_code(row["指标编号"])
            # 快照字段「告警级别」保持发生时的值；「当前告警级别」与列表读同一份。
            item["当前告警级别"] = self.current_alert_level(metric) if metric else row["告警级别"]
            result.append(item)
        result.sort(key=lambda row: row["发生时间"], reverse=True)
        return result

    def _metric_by_code(self, code: str | None) -> dict[str, Any] | None:
        for metric in self._metrics():
            if metric.get("指标编号") == code:
                return metric
        return None

    # ------------------------------------------------------------------ 冲突裁决
    def _conflict_key(self, metric: dict[str, Any]) -> tuple[int, int, str]:
        return (
            -_priority_rank(metric.get("业务优先级")),
            -_alert_rank(self.current_alert_level(metric)),
            str(metric.get("指标编号") or ""),
        )

    def _object_metric(self, object_name: str) -> dict[str, Any] | None:
        """同一监控对象的代表指标：业务优先级高者优先，其次告警级别高者。"""
        metrics = [m for m in self._metrics() if m.get("监控对象") == object_name]
        if not metrics:
            return None
        enabled = [m for m in metrics if m.get("status") == "已启用"]
        candidates = enabled or metrics
        return sorted(candidates, key=self._conflict_key)[0]

    def _conflicting_metrics(self, object_name: str) -> list[dict[str, Any]]:
        metrics = [
            m
            for m in self._metrics()
            if m.get("监控对象") == object_name and m.get("status") == "已启用"
        ]
        violating: list[dict[str, Any]] = []
        for metric in metrics:
            latest = self._latest_detection(metric)
            if latest and latest["判定"] == "异常":
                violating.append(metric)
        return violating if len(violating) >= 2 else []

    def resolve_conflicts(self, *, object_name: str | None = None) -> list[dict[str, Any]]:
        objects = sorted({str(m.get("监控对象")) for m in self._metrics() if m.get("监控对象")})
        if object_name:
            objects = [object_name]
        groups: list[dict[str, Any]] = []
        for name in objects:
            violating = self._conflicting_metrics(name)
            if not violating:
                continue
            violating = sorted(violating, key=self._conflict_key)
            winner = violating[0]
            version = self.current_version(winner)
            bounds = self.metric_bounds(winner)
            top_priority = _priority_rank(winner.get("业务优先级"))
            same_priority = [m for m in violating if _priority_rank(m.get("业务优先级")) == top_priority]
            if len(same_priority) > 1:
                top_level = _alert_rank(self.current_alert_level(winner))
                same_level = [m for m in same_priority if _alert_rank(self.current_alert_level(m)) == top_level]
                reason = (
                    f"业务优先级相同（{winner.get('业务优先级')}），按告警级别取更高"
                    f"（{ ' > '.join(ALERT_LEVELS[::-1]) }）"
                )
                if len(same_level) > 1:
                    reason += "，级别相同再按指标编号靠前"
            else:
                reason = (
                    f"按业务优先级裁决：{winner.get('业务优先级')}"
                    f" 高于 {'、'.join(str(m.get('业务优先级')) for m in violating[1:])}"
                )
            groups.append({
                "监控对象": name,
                "冲突指标": [
                    {
                        "指标编号": m.get("指标编号"),
                        "指标类型": m.get("指标类型"),
                        "告警级别": self.current_alert_level(m),
                        "业务优先级": m.get("业务优先级"),
                        "实测值": (self._latest_detection(m) or {}).get("实测值"),
                        "阈值": (self._latest_detection(m) or {}).get("阈值"),
                    }
                    for m in violating
                ],
                "裁决指标": winner.get("指标编号"),
                "裁决指标类型": winner.get("指标类型"),
                "裁决告警级别": self.current_alert_level(winner),
                "裁决依据": reason,
                "生效阈值": threshold_display(bounds),
                "恢复阈值": threshold_display(bounds),
                "恢复策略": winner.get("恢复策略") or DEFAULT_RECOVERY,
                "阈值版本": (version or {}).get("版本号", ""),
            })
        return groups

    def list_recovery(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for metric in sorted(self._metrics(), key=lambda m: str(m.get("指标编号"))):
            version = self.current_version(metric)
            bounds = self.metric_bounds(metric)
            items.append({
                "指标编号": metric.get("指标编号"),
                "监控对象": metric.get("监控对象"),
                "告警级别": self.current_alert_level(metric),
                "业务优先级": metric.get("业务优先级"),
                "恢复策略": metric.get("恢复策略") or DEFAULT_RECOVERY,
                # 恢复执行读取当前生效版本，避免按旧阈值恢复。
                "生效阈值": threshold_display(bounds),
                "恢复阈值": threshold_display(bounds),
                "阈值版本": (version or {}).get("版本号", ""),
            })
        return items

    # ------------------------------------------------------------------ 同步与装饰
    def sync_object(self, object_name: str) -> None:
        metric = self._object_metric(object_name)
        if metric is None:
            return
        version = self.current_version(metric)
        if version is None:
            return
        bounds = self.metric_bounds(metric)
        display = threshold_display(bounds)
        for row in store.rows("gold"):
            if str(row.get("质量监控对象") or "") == object_name:
                row["质量阈值"] = display
                row["阈值版本"] = version["版本号"]
                row["告警级别"] = self.current_alert_level(metric)
                row["恢复阈值"] = display
        for row in store.rows("govern"):
            if str(row.get("治理对象") or "") == object_name:
                row["触发阈值"] = display
                row["阈值版本"] = version["版本号"]
                row["告警级别"] = self.current_alert_level(metric)
                row["恢复阈值"] = display

    def sync_all(self) -> None:
        objects = {str(m.get("监控对象")) for m in self._metrics() if m.get("监控对象")}
        for name in sorted(objects):
            self.sync_object(name)

    def decorate_metric(self, metric: dict[str, Any]) -> dict[str, Any]:
        item = dict(metric)
        version = self.current_version(metric)
        bounds = self.metric_bounds(metric)
        latest = self._latest_detection(metric)
        item["阈值版本"] = (version or {}).get("版本号", item.get("阈值版本", ""))
        item["生效阈值"] = threshold_display(bounds)
        item["当前告警级别"] = self.current_alert_level(metric)
        item["恢复阈值"] = threshold_display(bounds)
        item["最近检测日期"] = (latest or {}).get("检测日期", "")
        item["最近检测值"] = (latest or {}).get("实测值", "")
        item["最近判定"] = (latest or {}).get("判定", "")
        return item

    def decorate_gold(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        object_name = str(item.get("质量监控对象") or "")
        if object_name:
            metric = self._object_metric(object_name)
            version = self.current_version(metric) if metric else None
            if metric is not None and version is not None:
                item["质量阈值"] = threshold_display(self.metric_bounds(metric))
                item["阈值版本"] = version["版本号"]
                item["告警级别"] = self.current_alert_level(metric)
        return item

    def decorate_govern(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        object_name = str(item.get("治理对象") or "")
        metric = self._object_metric(object_name) if object_name else None
        version = self.current_version(metric) if metric else None
        if metric is not None and version is not None:
            item["触发阈值"] = threshold_display(self.metric_bounds(metric))
            item["阈值版本"] = version["版本号"]
            item["告警级别"] = self.current_alert_level(metric)
        return item

    def version_sync(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        objects = sorted({str(m.get("监控对象")) for m in self._metrics() if m.get("监控对象")})
        for name in objects:
            metric = self._object_metric(name)
            version = self.current_version(metric) if metric else None
            if metric is None or version is None:
                continue
            gold = [self.decorate_gold(row) for row in store.rows("gold") if str(row.get("质量监控对象") or "") == name]
            govern = [self.decorate_govern(row) for row in store.rows("govern") if str(row.get("治理对象") or "") == name]
            codes = {item.get("阈值版本") for item in gold + govern if item.get("阈值版本")}
            items.append({
                "监控对象": name,
                "阈值版本": version["版本号"],
                "阈值": version["阈值范围"],
                "告警级别": self.current_alert_level(metric),
                "聚合视图": [{"视图编号": row.get("视图编号"), "阈值版本": row.get("阈值版本")} for row in gold],
                "治理规则": [{"规则编号": row.get("规则编号"), "阈值版本": row.get("阈值版本")} for row in govern],
                "一致": codes == {version["版本号"]},
            })
        return items

    # ------------------------------------------------------------------ 变更
    def change_threshold(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None, bool]:
        """调整阈值：生成/复用版本，按新口径重判，并同步聚合视图与治理规则。"""
        metric = store.find(QUALITY_MODULE, entry_id)
        if metric is None:
            return None, f"质量指标 {entry_id} 不存在或已归档", None, False

        raw_threshold = values.get("阈值范围", values.get("threshold"))
        if raw_threshold is None or not str(raw_threshold).strip():
            # 只调整告警级别 / 业务优先级时沿用当前阈值，同样走版本化。
            bounds = self.metric_bounds(metric)
        else:
            bounds, error = parse_threshold(raw_threshold)
            if error:
                return metric, error, None, False
        assert bounds is not None
        display = threshold_display(bounds)

        level_raw = str(values.get("告警级别", values.get("alert_level", "")) or "").strip()
        alert_level = level_raw or self.current_alert_level(metric)
        if alert_level not in ALERT_LEVELS:
            return metric, f"告警级别「{alert_level}」不在允许范围：{'、'.join(ALERT_LEVELS)}，未保存", None, False

        priority_raw = str(values.get("业务优先级", values.get("business_priority", "")) or "").strip()
        priority = priority_raw or str(metric.get("业务优先级") or DEFAULT_PRIORITY)

        current_bounds = self.metric_bounds(metric)
        current_version = self.current_version(metric)

        # 重复提交：目标阈值、告警级别与业务优先级都与当前生效值一致时复用当前版本。
        if (
            bounds == current_bounds
            and alert_level == self.current_alert_level(metric)
            and priority == str(metric.get("业务优先级") or DEFAULT_PRIORITY)
        ):
            code = (current_version or {}).get("版本号", "")
            return metric, f"重复提交，沿用当前版本 {code}", current_version, True

        fingerprint = self._fingerprint(metric, current_bounds, bounds, alert_level, priority)
        existing = self._fingerprints.get(fingerprint)
        if existing is not None and existing["状态"] == "生效":
            return metric, f"重复提交，沿用版本 {existing['版本号']}", existing, True

        note = str(values.get("备注", values.get("remark", "")) or "").strip()
        version = self._create_version(
            metric,
            bounds,
            alert_level,
            priority,
            previous=current_version,
            fingerprint=fingerprint,
            change_type="阈值调整",
            note=note,
        )
        metric["阈值范围"] = version["阈值范围"]
        metric["告警级别"] = alert_level
        metric["业务优先级"] = priority
        metric["阈值版本"] = version["版本号"]
        self._fingerprints[fingerprint] = version

        # 活跃指标按新口径重判：新增批次，历史批次原样保留。
        # 未启用的指标只登记版本，不产生检测批次；没有实测数据的指标也不凭空造值。
        judged = ""
        latest = self._latest_detection(metric)
        if metric.get("status") == "已启用" and latest is not None:
            detection = self._append_detection(
                metric,
                bounds,
                version["版本号"],
                float(latest["实测值"]),
                date.today(),
                batch_kind="新批次",
            )
            judged = f"，新批次判定为{detection['判定']}"
        elif metric.get("status") != "已启用":
            judged = "，指标未启用，本次只登记版本"
        else:
            judged = "，指标暂无检测数据，待下一批次按新口径判定"
        self.refresh_recovery(metric, bounds, version["版本号"])
        self.sync_object(str(metric.get("监控对象") or ""))
        return (
            metric,
            f"阈值已调整为 {display}，生成版本 {version['版本号']}{judged}",
            version,
            False,
        )

    def register_metric(self, metric: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """新指标登记时生成基线版本；阈值不合法则拒绝登记。"""
        self._normalize_metric(metric)
        bounds, error = parse_threshold(metric.get("阈值范围"))
        if error:
            return None, error
        assert bounds is not None
        version = self._create_version(
            metric,
            bounds,
            self.current_alert_level(metric),
            str(metric.get("业务优先级") or DEFAULT_PRIORITY),
            change_type="初始化",
        )
        metric["阈值范围"] = version["阈值范围"]
        metric["阈值版本"] = version["版本号"]
        self.sync_object(str(metric.get("监控对象") or ""))
        return version, None

    # ------------------------------------------------------------------ 初始化
    def ensure_seeded(self) -> None:
        if self._seeded:
            return
        for metric in self._metrics():
            self._normalize_metric(metric)
            bounds, _ = parse_threshold(metric.get("阈值范围"))
            if bounds is None:
                bounds = (0.95, 0.95)
                metric["阈值范围"] = threshold_display(bounds)
            if not metric.get("阈值版本"):
                version = self._create_version(
                    metric,
                    bounds,
                    self.current_alert_level(metric),
                    str(metric.get("业务优先级") or DEFAULT_PRIORITY),
                    change_type="初始化",
                )
                metric["阈值版本"] = version["版本号"]
            self._seed_history(metric, bounds, str(metric["阈值版本"]))
            # 初始化版本与七天历史都按当时阈值落好之后，再迁移到新口径，
            # 这样新批次和聚合视图/治理规则读新版本，历史批次仍保留旧阈值。
            adjustment = _BOOTSTRAP_ADJUSTMENTS.get(str(metric.get("指标编号") or ""))
            if adjustment is not None:
                self.change_threshold(int(metric.get("id", 0)), dict(adjustment))
        self._seeded = True
        self.sync_all()

    def _seed_history(self, metric: dict[str, Any], bounds: tuple[float, float], version_code: str) -> None:
        metric_id = int(metric.get("id", 0))
        values = _SEED_VALUES.get(metric_id, [0.94, 0.95, 0.96, 0.93, 0.97, 0.95, 0.94])
        today = date.today()
        for offset in range(HISTORY_DAYS):
            day = today - timedelta(days=HISTORY_DAYS - 1 - offset)
            value = values[offset % len(values)]
            self._append_detection(
                metric,
                bounds,
                version_code,
                value,
                day,
                batch_kind="历史",
            )


quality_registry = QualityRegistry()
