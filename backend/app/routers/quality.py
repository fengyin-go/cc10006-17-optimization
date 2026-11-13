"""质量监控接口：维护质量指标，覆盖提交校验、启用监控、停用监控与阈值版本化。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.quality import QualityService
from app.services.quality_registry import HISTORY_DAYS

router = APIRouter(prefix="/api/quality", tags=["质量监控"])

service = QualityService()

LIST_FIELDS = [
    "指标编号", "监控对象", "指标类型", "阈值范围", "检测周期", "告警级别", "通知渠道", "恢复策略", "业务优先级",
]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def _invalid_threshold(message: str, entry: dict[str, Any] | None = None) -> JSONResponse:
    """阈值超出允许范围：明确拒绝并且不落库。"""
    return JSONResponse(
        status_code=400,
        content={"ok": False, "message": message, "entry": entry, "version": None, "duplicate": False},
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按指标编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    object_name: str | None = Query(default=None, alias="监控对象", description="按监控对象精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按指标编号与状态过滤质量监控列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, object_name=object_name, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出质量监控清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "quality", "total": total, "items": items}


@router.get("/versions")
def list_versions(
    metric_id: int | None = Query(default=None, description="按指标 id 过滤"),
    object_name: str | None = Query(default=None, alias="监控对象", description="按监控对象过滤"),
) -> dict[str, Any]:
    """阈值版本台账：每次调整一条，历史版本保留调整时的阈值。"""
    items = service.list_versions(metric_id=metric_id, object_name=object_name)
    return {"total": len(items), "items": items}


@router.get("/results")
def list_results(
    metric_id: int | None = Query(default=None, description="按指标 id 过滤"),
    object_name: str | None = Query(default=None, alias="监控对象", description="按监控对象过滤"),
    days: int = Query(default=HISTORY_DAYS, ge=1, le=365, description="保留最近多少天的检测结果"),
) -> dict[str, Any]:
    """检测结果：历史批次保留当时阈值与告警级别，新批次按当前生效版本判定。"""
    items = service.list_results(metric_id=metric_id, object_name=object_name, days=days)
    return {"total": len(items), "days": days, "items": items}


@router.get("/alerts")
def list_alerts(
    metric_id: int | None = Query(default=None, description="按指标 id 过滤"),
    object_name: str | None = Query(default=None, alias="监控对象", description="按监控对象过滤"),
    status: str | None = Query(default=None, description="处理中、已恢复"),
) -> dict[str, Any]:
    """告警通知：触发阈值与告警级别是发生时的快照，恢复阈值读取当前生效版本。"""
    items = service.list_alerts(metric_id=metric_id, object_name=object_name, status=status)
    return {"total": len(items), "items": items}


@router.get("/conflicts")
def list_conflicts(
    object_name: str | None = Query(default=None, alias="监控对象", description="按监控对象过滤"),
) -> dict[str, Any]:
    """同一监控对象多个指标冲突时的裁决结果：业务优先级为准，其次告警级别。"""
    items = service.resolve_conflicts(object_name=object_name)
    return {"total": len(items), "items": items}


@router.get("/recovery")
def list_recovery() -> dict[str, Any]:
    """恢复策略：按当前生效版本计算恢复阈值，避免按旧阈值恢复。"""
    items = service.list_recovery()
    return {"total": len(items), "items": items}


@router.get("/version-sync")
def version_sync() -> dict[str, Any]:
    """版本同步视图：确认聚合视图与治理规则都读到了同一个生效版本。"""
    items = service.version_sync()
    return {"total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量指标明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量指标 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量指标，缺字段时说明原因而不是静默丢弃。"""
    entry, missing, message = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        raise HTTPException(status_code=404, detail=message or "质量指标登记失败")
    if message:
        # 阈值不合法时 register_metric 返回错误文案，此时没有版本号也没有落库。
        return _invalid_threshold(message, entry)
    return ActionResult(ok=True, message="质量指标已登记", entry=entry)


@router.post("/{entry_id}/threshold", response_model=ActionResult)
def change_threshold(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整阈值：生成新版本、按新口径重判活跃指标，并同步聚合视图与治理规则。

    - 超出 0~1 范围的阈值直接拒绝，不保存；
    - 重复提交同一次修改只返回一个生效版本，不重复生成版本号；
    - 已发生告警保留触发时的阈值与告警级别，活跃告警的恢复阈值按新版本刷新。
    """
    entry, message, version, duplicate, error = service.change_threshold(entry_id, payload.values)
    if entry is None:
        raise HTTPException(status_code=404, detail=message)
    if error:
        return _invalid_threshold(error, entry)
    return ActionResult(ok=True, message=message, entry=entry, version=version, duplicate=duplicate)


@router.put("/{entry_id}/threshold", response_model=ActionResult)
def update_threshold(entry_id: int, payload: EntryPayload) -> ActionResult:
    """与 POST 同一入口：阈值调整不因提交方法不同而产生第二份口径。"""
    return change_threshold(entry_id, payload)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条质量指标执行提交校验、启用监控、停用监控；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
