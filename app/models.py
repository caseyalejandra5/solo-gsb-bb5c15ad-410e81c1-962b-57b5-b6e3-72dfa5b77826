"""请求 / 响应数据模型。

标记物浓度支持两种等价写法：

* ``concentration``：单一标记物（内部记名为 ``default``）；
* ``markers``：``{标记物名: 浓度}``，可同时追踪多种标记物。

转移时间既可以是数值型序号（``1``、``2.5``），也可以是 ISO 8601 字符串
（``2026-09-18T10:00:00Z``）；同一批转移必须按时间非递减排列。

每次转移的 ``amount`` 是从源管吸取的体积；可选 ``delivered_amount``
（``0 < delivered_amount <= amount``）表示实际注入目标管的体积，差额视为
移液损耗并累计到结果的 ``waste`` 中。不传 ``delivered_amount`` 时默认无损耗。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# 数值时间 / ISO 8601 时间均可
TimeValue = Union[int, float, str]


def parse_time(value: TimeValue) -> float:
    """把输入时间统一转换为可比较的浮点数。

    数值直接转 float；字符串先尝试按数字解析，再按 ISO 8601 解析为
    时区感知的 Unix 时间戳（无时区后缀时按 UTC 处理）。
    """
    if isinstance(value, bool):
        raise ValueError("time 必须是数值序号或 ISO 8601 字符串，不能是布尔值")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("数值型 time 必须为有限且非负的数")
        return number
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            if not math.isfinite(number) or number < 0:
                raise ValueError("数值型 time 必须为有限且非负的数")
            return number
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"time {value!r} 无法解析：请使用数值序号或 ISO 8601 字符串"
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError("time 必须是数值序号或 ISO 8601 字符串")


def _finite_number(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} 必须为有限数值")
    return value


class SampleIn(BaseModel):
    """一支初始样本管。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128, description="样本管唯一编号")
    initial_volume: float = Field(..., gt=0, description="初始体积（任意体积单位，需全局一致）")
    concentration: Optional[float] = Field(
        default=None, ge=0, description="单一标记物的初始浓度（单位体积含量）"
    )
    markers: Optional[dict[str, float]] = Field(
        default=None, description="多种标记物的初始浓度，键为标记物名称"
    )
    note: Optional[str] = Field(default=None, max_length=512)

    @field_validator("id")
    @classmethod
    def _strip_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("样本 id 不能为空字符串")
        return value

    @field_validator("initial_volume", "concentration")
    @classmethod
    def _finite(cls, value: Optional[float]) -> Optional[float]:
        if value is not None:
            _finite_number(value, "数值")
        return value

    @field_validator("markers")
    @classmethod
    def _validate_markers(cls, markers: Optional[dict[str, float]]) -> Optional[dict[str, float]]:
        if markers is None:
            return None
        if not markers:
            raise ValueError("markers 提供时必须是非空对象")
        normalized: dict[str, float] = {}
        for name, raw in markers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("标记物名称必须是非空字符串")
            value = float(raw)
            _finite_number(value, f"标记物 {name!r} 的浓度")
            if value < 0:
                raise ValueError(f"标记物 {name!r} 的浓度不能为负")
            normalized[name.strip()] = value
        return normalized

    @model_validator(mode="after")
    def _require_concentration(self) -> "SampleIn":
        if self.concentration is None and not self.markers:
            raise ValueError("每个样本必须提供 concentration 或非空 markers（至少一种标记物）")
        return self

    def normalized_markers(self) -> dict[str, float]:
        """合并后的标记物浓度；``concentration`` 归入 ``default``。"""
        markers = dict(self.markers or {})
        if self.concentration is not None:
            markers.setdefault("default", self.concentration)
        return markers


class TransferIn(BaseModel):
    """一次从源管到目标管的定量转移（转移前默认管内已充分混匀）。

    ``amount`` 表示从源管**吸取**的体积；可选的 ``delivered_amount`` 表示
    实际注入目标管的体积（模拟移液枪挂壁 / 残液等损耗），满足
    ``0 < delivered_amount <= amount``。差额 ``amount - delivered_amount``
    计入废液（waste），按源管当时的浓度与来源构成追踪。
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = Field(default=None, max_length=128, description="可选的转移操作编号")
    time: TimeValue = Field(..., description="操作时间：数值序号或 ISO 8601 字符串")
    source: str = Field(..., min_length=1, description="源样本管 id")
    target: str = Field(..., min_length=1, description="目标样本管 id")
    amount: float = Field(..., gt=0, description="从源管吸取的体积，不得大于源管当前可用体积")
    delivered_amount: Optional[float] = Field(
        default=None,
        gt=0,
        description=(
            "实际注入目标管的体积；必须满足 0 < delivered_amount <= amount。"
            "不传时默认等于 amount（无移液损耗）。"
        ),
    )

    @field_validator("source", "target")
    @classmethod
    def _strip_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source / target 不能为空字符串")
        return value

    @field_validator("time")
    @classmethod
    def _valid_time(cls, value: TimeValue) -> TimeValue:
        parse_time(value)  # 提前校验，错误会定位到该字段
        return value

    @field_validator("amount", "delivered_amount")
    @classmethod
    def _finite_amount(cls, value: Optional[float]) -> Optional[float]:
        if value is not None:
            _finite_number(value, "数值")
        return value

    @model_validator(mode="after")
    def _delivered_not_above_amount(self) -> "TransferIn":
        if (
            self.delivered_amount is not None
            and self.delivered_amount > self.amount
        ):
            raise ValueError(
                f"delivered_amount（实际注入体积 {self.delivered_amount}）不能大于 "
                f"amount（吸取体积 {self.amount}）：移液损耗只可能让目标管收到更少"
            )
        return self


class SimulationRequest(BaseModel):
    """一次完整模拟请求：初始样本 + 按时间排序的转移序列。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=256, description="本次模拟的名称")
    samples: list[SampleIn] = Field(..., min_length=1, description="初始样本管列表")
    transfers: list[TransferIn] = Field(default_factory=list, description="按时间排序的转移操作")

    @model_validator(mode="after")
    def _unique_sample_ids(self) -> "SimulationRequest":
        seen: set[str] = set()
        duplicates: list[str] = []
        for sample in self.samples:
            if sample.id in seen and sample.id not in duplicates:
                duplicates.append(sample.id)
            seen.add(sample.id)
        if duplicates:
            raise ValueError(f"样本 id 重复：{duplicates}")
        return self


# ---------- 响应模型（用于 OpenAPI 文档） ----------


class OriginOut(BaseModel):
    source_id: str
    volume: float
    volume_fraction: float


class TubeStateOut(BaseModel):
    id: str
    volume: float
    concentrations: dict[str, float]
    origins: list[OriginOut]


class TransferredOut(BaseModel):
    volume: float
    concentrations: dict[str, float]
    origins: list[OriginOut]


class CompositionOut(BaseModel):
    """一段液体（损耗 / 累计废液）的体积、浓度、质量与来源构成。"""

    volume: float
    concentrations: dict[str, float]
    marker_amount: dict[str, float]
    origins: list[OriginOut]


class TubeVolumeChangeOut(BaseModel):
    """某一步中单支管的体积变化。"""

    tube_id: str
    volume_before: float
    volume_after: float
    volume_change: float


class ConservationOut(BaseModel):
    # 管内口径
    tube_volume_before: float
    tube_volume_after: float
    tube_volume_change: float
    tube_marker_amount_before: dict[str, float]
    tube_marker_amount_after: dict[str, float]
    tube_marker_amount_change: dict[str, float]
    # 管内体积变化的逐管明细
    tube_volume_changes: list[TubeVolumeChangeOut]
    # 系统口径：管内 + 累计废液
    waste_volume_before: float
    waste_volume_after: float
    waste_marker_amount_before: dict[str, float]
    waste_marker_amount_after: dict[str, float]
    system_volume_before: float
    system_volume_after: float
    # 系统总量差：(管内 + 累计废液) 之后 − 之前，守恒时为 0
    system_volume_difference: float
    system_marker_amount_before: dict[str, float]
    system_marker_amount_after: dict[str, float]
    system_marker_amount_difference: dict[str, float]


class StepOut(BaseModel):
    index: int
    transfer_id: Optional[str]
    time: TimeValue
    source: str
    target: str
    amount: float
    delivered_amount: float
    transferred: TransferredOut
    lost: CompositionOut
    waste_after: CompositionOut
    source_after: TubeStateOut
    target_after: TubeStateOut
    conservation: ConservationOut


class GlobalConservationOut(BaseModel):
    # 管内口径
    initial_total_volume: float
    final_total_volume: float
    volume_difference: float
    initial_marker_amount: dict[str, float]
    final_marker_amount: dict[str, float]
    marker_amount_difference: dict[str, float]
    # 管内体积变化的逐管明细
    tube_volume_changes: list[TubeVolumeChangeOut]
    # 累计废液
    waste_total_volume: float
    waste_marker_amount: dict[str, float]
    # 系统口径：管内 + 累计废液，差值在守恒时为 0
    system_initial_volume: float
    system_final_volume: float
    system_volume_difference: float
    system_initial_marker_amount: dict[str, float]
    system_final_marker_amount: dict[str, float]
    system_marker_amount_difference: dict[str, float]


class ErrorDetail(BaseModel):
    code: str
    message: str
    location: Optional[str] = None
    transfer_index: Optional[int] = None


class SimulationResult(BaseModel):
    id: str
    status: str
    name: Optional[str] = None
    created_at: str
    initial_states: list[TubeStateOut]
    steps: list[StepOut]
    final_states: list[TubeStateOut]
    waste: Optional[CompositionOut] = None
    conservation: Optional[GlobalConservationOut] = None
    error: Optional[ErrorDetail] = None


class SimulationSummary(BaseModel):
    id: str
    name: Optional[str] = None
    status: str
    error_code: Optional[str] = None
    step_count: int
    created_at: str


class SimulationList(BaseModel):
    items: list[SimulationSummary]
    limit: int
    offset: int
