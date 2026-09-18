"""混样模拟核心引擎。

物理模型
--------

* 每次转移前假设源管内已充分混匀，转出的液体携带源管当前的混合浓度与
  来源构成；
* ``amount`` 表示从源管**吸取**的体积；可选的 ``delivered_amount``
  （``0 < delivered_amount <= amount``）表示实际注入目标管的体积。差额
  ``amount - delivered_amount`` 视为移液损耗（挂壁、枪头残液等），按源管
  当时的浓度与来源构成计入废液（waste）；
* 超量校验以吸取量 ``amount`` 为准：``amount`` 不得超过源管当前可用体积；
* 体积守恒（系统口径）：``V_total = sum(各管体积) + 累计废液体积`` 全程不变；
* 标记物质量守恒（系统口径）：对每种标记物，
  ``sum(管内体积 × 该管浓度) + 废液中该标记物质量`` 全程不变；
* 仅看管内口径时，转移后管内总体会减少一个损耗体积，该变化逐管 / 逐标记物
  在 ``conservation.tube_*`` 中给出，便于核对；
* 来源占比按“来自各初始样本管的净体积 / 当前体积”计算（废液同理）。

失败时（超量、时间逆序、未知样本）返回已完成的步骤、当时各管状态和
结构化错误信息，便于定位问题，不抛出业务异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import SampleIn, SimulationRequest, TransferIn, parse_time

# 浮点清理精度：小于该阈值的残余量按 0 处理
ZERO_EPS = 1e-12
ROUND_DIGITS = 12


class SimulationError(Exception):
    """输入层面的错误（理论上由校验器拦截，引擎内作为兜底）。"""

    def __init__(self, code: str, message: str, location: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.location = location


@dataclass
class _Tube:
    """引擎内部使用的管状态。"""

    id: str
    volume: float
    # 每种标记物的质量（浓度 × 体积），混匀计算都在质量域进行
    marker_amount: dict[str, float]
    # 来源构成：初始样本 id -> 净转入体积
    origins: dict[str, float] = field(default_factory=dict)


def _clean(value: float) -> float:
    """统一浮点输出：吞掉接近 0 的残差并截断噪声。"""
    if abs(value) < ZERO_EPS:
        return 0.0
    return round(value, ROUND_DIGITS)


def _marker_dict(values: dict[str, float], all_markers: list[str]) -> dict[str, float]:
    """按全局标记物并集输出清理后的质量字典。"""
    return {name: _clean(values.get(name, 0.0)) for name in all_markers}


def _concentrations_from(
    volume: float, marker_amount: dict[str, float], all_markers: list[str]
) -> dict[str, float]:
    if volume <= ZERO_EPS:
        return {name: 0.0 for name in all_markers}
    return {
        name: _clean(marker_amount.get(name, 0.0) / volume)
        for name in all_markers
    }


def _concentrations(tube: _Tube, all_markers: list[str]) -> dict[str, float]:
    return _concentrations_from(tube.volume, tube.marker_amount, all_markers)


def _origins_view(origins: dict[str, float], total_volume: float) -> list[dict]:
    """来源构成视图（占比之和恒为 1，体积为 0 时为空列表）。"""
    out: list[dict] = []
    if total_volume > ZERO_EPS:
        for source_id in sorted(origins):
            vol = origins[source_id]
            if vol <= ZERO_EPS:
                continue
            out.append(
                {
                    "source_id": source_id,
                    "volume": _clean(vol),
                    "volume_fraction": _clean(vol / total_volume),
                }
            )
    return out


def _composition_view(
    volume: float,
    marker_amount: dict[str, float],
    origins: dict[str, float],
    all_markers: list[str],
) -> dict:
    """废液 / 损耗液的统一视图：体积 + 浓度 + 质量 + 来源构成。"""
    return {
        "volume": _clean(volume),
        "concentrations": _concentrations_from(volume, marker_amount, all_markers),
        "marker_amount": _marker_dict(marker_amount, all_markers),
        "origins": _origins_view(origins, volume),
    }


def _snapshot(tube: _Tube, all_markers: list[str]) -> dict:
    """对外的管状态视图（来源占比之和恒为 1，空管除外）。"""
    return {
        "id": tube.id,
        "volume": _clean(tube.volume),
        "concentrations": _concentrations(tube, all_markers),
        "origins": _origins_view(tube.origins, tube.volume),
    }


def _total_marker_amount(
    tubes: dict[str, _Tube], all_markers: list[str]
) -> dict[str, float]:
    return {
        name: _clean(sum(t.marker_amount.get(name, 0.0) for t in tubes.values()))
        for name in all_markers
    }


def _tube_volume_change(tube_id: str, before: float, after: float) -> dict:
    return {
        "tube_id": tube_id,
        "volume_before": _clean(before),
        "volume_after": _clean(after),
        "volume_change": _clean(after - before),
    }


def _marker_diff(
    after: dict[str, float], before: dict[str, float], all_markers: list[str]
) -> dict[str, float]:
    return {
        name: _clean(after.get(name, 0.0) - before.get(name, 0.0))
        for name in all_markers
    }


def _step_conservation(
    *,
    tube_vol_before: float,
    tube_vol_after: float,
    tube_marker_before: dict[str, float],
    tube_marker_after: dict[str, float],
    tube_changes: list[dict],
    waste_vol_before: float,
    waste_vol_after: float,
    waste_marker_before: dict[str, float],
    waste_marker_after: dict[str, float],
    all_markers: list[str],
) -> dict:
    """单步守恒：管内口径、废液变化与系统口径（管内 + 累计废液）。"""
    sys_vol_before = tube_vol_before + waste_vol_before
    sys_vol_after = tube_vol_after + waste_vol_after
    sys_marker_before = {
        name: tube_marker_before.get(name, 0.0) + waste_marker_before.get(name, 0.0)
        for name in all_markers
    }
    sys_marker_after = {
        name: tube_marker_after.get(name, 0.0) + waste_marker_after.get(name, 0.0)
        for name in all_markers
    }
    return {
        "tube_volume_before": _clean(tube_vol_before),
        "tube_volume_after": _clean(tube_vol_after),
        "tube_volume_change": _clean(tube_vol_after - tube_vol_before),
        "tube_marker_amount_before": tube_marker_before,
        "tube_marker_amount_after": tube_marker_after,
        "tube_marker_amount_change": _marker_diff(
            tube_marker_after, tube_marker_before, all_markers
        ),
        "tube_volume_changes": tube_changes,
        "waste_volume_before": _clean(waste_vol_before),
        "waste_volume_after": _clean(waste_vol_after),
        "waste_marker_amount_before": waste_marker_before,
        "waste_marker_amount_after": waste_marker_after,
        "system_volume_before": _clean(sys_vol_before),
        "system_volume_after": _clean(sys_vol_after),
        "system_volume_difference": _clean(sys_vol_after - sys_vol_before),
        "system_marker_amount_before": _marker_dict(sys_marker_before, all_markers),
        "system_marker_amount_after": _marker_dict(sys_marker_after, all_markers),
        "system_marker_amount_difference": _marker_diff(
            sys_marker_after, sys_marker_before, all_markers
        ),
    }


def _build_error(
    code: str, message: str, transfer_index: int, location: str
) -> dict:
    return {
        "code": code,
        "message": message,
        "transfer_index": transfer_index,
        "location": location,
    }


def run_simulation(request: SimulationRequest) -> dict:
    """执行整次模拟，返回可直接序列化的结果字典（含 status / error）。"""
    samples: list[SampleIn] = request.samples
    transfers: list[TransferIn] = request.transfers

    # 所有出现过的标记物取并集，保证每支管子的浓度字段完整
    marker_set: set[str] = set()
    for sample in samples:
        marker_set.update(sample.normalized_markers().keys())
    all_markers = sorted(marker_set)

    tubes: dict[str, _Tube] = {}
    for sample in samples:
        concentrations = sample.normalized_markers()
        tubes[sample.id] = _Tube(
            id=sample.id,
            volume=sample.initial_volume,
            marker_amount={
                name: concentrations.get(name, 0.0) * sample.initial_volume
                for name in all_markers
            },
            origins={sample.id: sample.initial_volume},
        )

    initial_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    initial_total_volume = sum(s.initial_volume for s in samples)
    initial_marker_amount = _total_marker_amount(tubes, all_markers)

    # —— 累计移液损耗（废液）：质量域计算，来源单独追踪 ——
    waste_volume = 0.0
    waste_marker: dict[str, float] = {name: 0.0 for name in all_markers}
    waste_origins: dict[str, float] = {}

    steps: list[dict] = []
    error: Optional[dict] = None
    last_time: Optional[float] = None

    for index, transfer in enumerate(transfers):
        loc = f"transfers[{index}]"

        if transfer.source == transfer.target:
            error = _build_error(
                "SAME_SOURCE_TARGET",
                f"转移的源管与目标管相同（均为 {transfer.source!r}），不允许自转移",
                index,
                loc,
            )
            break

        source = tubes.get(transfer.source)
        if source is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的源管 "
                f"{transfer.source!r}",
                index,
                f"{loc}.source",
            )
            break

        target = tubes.get(transfer.target)
        if target is None:
            error = _build_error(
                "UNKNOWN_SAMPLE",
                f"transfers[{index}] 引用了未在 samples 中声明的目标管 "
                f"{transfer.target!r}",
                index,
                f"{loc}.target",
            )
            break

        current_time = parse_time(transfer.time)
        if last_time is not None and current_time < last_time - ZERO_EPS:
            error = _build_error(
                "TIME_NOT_ORDERED",
                f"transfers[{index}] 的时间 {transfer.time!r} 早于上一次转移的时间"
                f"（transfers[{index - 1}]），转移必须按时间非递减排序",
                index,
                f"{loc}.time",
            )
            break

        # 超量按“吸取量 amount”校验
        available = source.volume
        if transfer.amount > available + ZERO_EPS:
            error = _build_error(
                "INSUFFICIENT_VOLUME",
                f"transfers[{index}] 试图从 {transfer.source!r} 吸取 "
                f"{transfer.amount}，但该管当前仅有 {_clean(available)}",
                index,
                f"{loc}.amount",
            )
            break

        amount = min(transfer.amount, available)
        # 旧请求未传 delivered_amount 时视为无损
        delivered = (
            amount if transfer.delivered_amount is None
            else min(transfer.delivered_amount, amount)
        )
        lost_volume = amount - delivered

        # —— 转移前快照（用于守恒校验与转出液描述）——
        tube_vol_before = sum(t.volume for t in tubes.values())
        tube_marker_before = _total_marker_amount(tubes, all_markers)
        waste_vol_before = waste_volume
        waste_marker_before = _marker_dict(waste_marker, all_markers)
        source_vol_before = source.volume
        target_vol_before = target.volume
        source_concentrations = _concentrations(source, all_markers)

        # 吸取液的来源构成：按源管各来源占比分摊吸取体积；
        # 再拆成“实际入目标管”与“损耗”两部分
        aspirated_origins: list[dict] = []
        lost_origins_map: dict[str, float] = {}
        if available > ZERO_EPS:
            for src_id in sorted(source.origins):
                share = source.origins[src_id] / available * amount
                if share <= ZERO_EPS:
                    continue
                aspirated_origins.append(
                    {
                        "source_id": src_id,
                        "volume": _clean(share),
                        "volume_fraction": _clean(share / amount) if amount else 0.0,
                    }
                )
                lost_share = share - share / amount * delivered if amount else 0.0
                if lost_share > ZERO_EPS:
                    lost_origins_map[src_id] = lost_share

        lost_marker_step = {
            name: source_concentrations[name] * lost_volume for name in all_markers
        }

        # —— 更新质量与体积 ——
        # 源管损失整支吸取液；目标管只收到 delivered；差额进入废液
        for name in all_markers:
            aspirated_mass = source_concentrations[name] * amount
            delivered_mass = source_concentrations[name] * delivered
            source.marker_amount[name] -= aspirated_mass
            target.marker_amount[name] += delivered_mass
            waste_marker[name] += aspirated_mass - delivered_mass

        source.volume -= amount
        target.volume += delivered
        waste_volume += lost_volume

        # —— 更新来源构成（源管按比例扣减；目标管与废液分别累加各自份额）——
        if available > ZERO_EPS:
            ratio = amount / available
            for src_id, src_vol in list(source.origins.items()):
                share = src_vol * ratio
                delivered_share = share / amount * delivered if amount else 0.0
                source.origins[src_id] = src_vol - share
                target.origins[src_id] = target.origins.get(src_id, 0.0) + delivered_share
                lost_share = share - delivered_share
                if lost_share > ZERO_EPS:
                    waste_origins[src_id] = waste_origins.get(src_id, 0.0) + lost_share

        tube_vol_after = sum(t.volume for t in tubes.values())
        tube_marker_after = _total_marker_amount(tubes, all_markers)

        # 本步损耗视图（体积为 0 时浓度 / 来源均为零值语义）
        lost_view = {
            "volume": _clean(lost_volume),
            "concentrations": (
                dict(source_concentrations)
                if lost_volume > ZERO_EPS
                else {name: 0.0 for name in all_markers}
            ),
            "marker_amount": _marker_dict(lost_marker_step, all_markers),
            "origins": _origins_view(lost_origins_map, lost_volume),
        }

        steps.append(
            {
                "index": index,
                "transfer_id": transfer.id,
                "time": transfer.time,
                "source": transfer.source,
                "target": transfer.target,
                "amount": _clean(amount),
                "delivered_amount": _clean(delivered),
                "transferred": {
                    "volume": _clean(amount),
                    "concentrations": {
                        name: _clean(source_concentrations[name]) for name in all_markers
                    },
                    "origins": aspirated_origins,
                },
                "lost": lost_view,
                "waste_after": _composition_view(
                    waste_volume, waste_marker, waste_origins, all_markers
                ),
                "source_after": _snapshot(source, all_markers),
                "target_after": _snapshot(target, all_markers),
                "conservation": _step_conservation(
                    tube_vol_before=tube_vol_before,
                    tube_vol_after=tube_vol_after,
                    tube_marker_before=tube_marker_before,
                    tube_marker_after=tube_marker_after,
                    tube_changes=[
                        _tube_volume_change(
                            transfer.source, source_vol_before, source.volume
                        ),
                        _tube_volume_change(
                            transfer.target, target_vol_before, target.volume
                        ),
                    ],
                    waste_vol_before=waste_vol_before,
                    waste_vol_after=waste_volume,
                    waste_marker_before=waste_marker_before,
                    waste_marker_after=_marker_dict(waste_marker, all_markers),
                    all_markers=all_markers,
                ),
            }
        )
        last_time = current_time

    final_states = [_snapshot(tubes[s.id], all_markers) for s in samples]
    final_total_volume = sum(t.volume for t in tubes.values())
    final_marker_amount = _total_marker_amount(tubes, all_markers)
    waste_view = _composition_view(
        waste_volume, waste_marker, waste_origins, all_markers
    )

    # 逐管的全程体积变化（初始 -> 终态）
    global_tube_changes = [
        _tube_volume_change(
            s.id, s.initial_volume, tubes[s.id].volume
        )
        for s in samples
    ]

    # 系统口径：管内 + 累计废液；初始废液为 0
    system_initial_volume = initial_total_volume
    system_final_volume = final_total_volume + waste_volume
    system_initial_marker = initial_marker_amount
    system_final_marker = {
        name: final_marker_amount.get(name, 0.0) + waste_marker.get(name, 0.0)
        for name in all_markers
    }

    conservation = {
        # —— 管内口径（旧字段，含义不变：仅统计各管内）——
        "initial_total_volume": _clean(initial_total_volume),
        "final_total_volume": _clean(final_total_volume),
        "volume_difference": _clean(final_total_volume - initial_total_volume),
        "initial_marker_amount": initial_marker_amount,
        "final_marker_amount": final_marker_amount,
        "marker_amount_difference": _marker_diff(
            final_marker_amount, initial_marker_amount, all_markers
        ),
        "tube_volume_changes": global_tube_changes,
        # —— 累计废液 ——
        "waste_total_volume": _clean(waste_volume),
        "waste_marker_amount": _marker_dict(waste_marker, all_markers),
        # —— 系统口径：管内 + 累计废液 ——
        "system_initial_volume": _clean(system_initial_volume),
        "system_final_volume": _clean(system_final_volume),
        "system_volume_difference": _clean(
            system_final_volume - system_initial_volume
        ),
        "system_initial_marker_amount": system_initial_marker,
        "system_final_marker_amount": _marker_dict(system_final_marker, all_markers),
        "system_marker_amount_difference": _marker_diff(
            system_final_marker, system_initial_marker, all_markers
        ),
    }

    return {
        "status": "failed" if error else "success",
        "name": request.name,
        "initial_states": initial_states,
        "steps": steps,
        "final_states": final_states,
        "waste": waste_view,
        "conservation": conservation,
        "error": error,
    }
