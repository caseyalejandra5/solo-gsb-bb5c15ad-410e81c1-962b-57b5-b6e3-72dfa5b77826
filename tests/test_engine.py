"""引擎层测试：混合浓度、来源占比、守恒与各类业务错误。"""
from app.engine import run_simulation
from app.models import SampleIn, SimulationRequest, TransferIn


def make_request():
    return SimulationRequest(
        name="demo",
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=40.0),
            TransferIn(time=2, source="B", target="A", amount=70.0),
        ],
    )


def test_basic_mixing_concentrations():
    result = run_simulation(make_request())

    assert result["status"] == "success"
    assert len(result["steps"]) == 2

    # 第一步：A(100, c=10) -> B 转移 40
    # B 变为 140，质量 400 => 浓度 400/140 = 2.8571...
    step1 = result["steps"][0]
    assert step1["source_after"]["volume"] == 60
    assert step1["source_after"]["concentrations"]["default"] == 10.0
    assert step1["target_after"]["volume"] == 140
    assert abs(step1["target_after"]["concentrations"]["default"] - 400 / 140) < 1e-9

    # 第二步：B(140, c=2.8571) -> A 转移 70（转出 B 的一半）
    step2 = result["steps"][1]
    assert step2["source_after"]["volume"] == 70
    assert step2["target_after"]["volume"] == 130
    # A：质量 600 + 200 = 800，体积 130
    assert abs(step2["target_after"]["concentrations"]["default"] - 800 / 130) < 1e-9
    # B 剩余：质量 200，体积 70
    assert abs(step2["source_after"]["concentrations"]["default"] - 200 / 70) < 1e-9


def test_origin_fractions_and_conservation():
    result = run_simulation(make_request())

    final_a = next(t for t in result["final_states"] if t["id"] == "A")
    final_b = next(t for t in result["final_states"] if t["id"] == "B")

    # 第二步后 A 的来源：原 A 剩 60 + 从 B 回流入 A 的 20(A)+50(B)
    # => A 来源 80，B 来源 50，总体积 130
    origins_a = {o["source_id"]: o for o in final_a["origins"]}
    assert abs(origins_a["A"]["volume"] - 80) < 1e-9
    assert abs(origins_a["B"]["volume"] - 50) < 1e-9
    assert abs(origins_a["A"]["volume_fraction"] - 80 / 130) < 1e-9
    assert abs(sum(o["volume_fraction"] for o in final_a["origins"]) - 1.0) < 1e-9

    # B 剩余：A 20 + B 50
    origins_b = {o["source_id"]: o for o in final_b["origins"]}
    assert abs(origins_b["A"]["volume"] - 20) < 1e-9
    assert abs(origins_b["B"]["volume"] - 50) < 1e-9

    conservation = result["conservation"]
    assert conservation["initial_total_volume"] == 200
    assert conservation["final_total_volume"] == 200
    assert conservation["volume_difference"] == 0
    assert conservation["initial_marker_amount"]["default"] == 1000
    assert conservation["final_marker_amount"]["default"] == 1000
    assert conservation["marker_amount_difference"]["default"] == 0

    for step in result["steps"]:
        # 单步守恒改为“管内 + 累计废液”的系统口径，差值恒为 0
        assert step["conservation"]["system_volume_difference"] == 0
        assert step["conservation"]["system_marker_amount_difference"]["default"] == 0
        # 无损耗请求中管内口径差值同样为 0
        assert step["conservation"]["tube_volume_change"] == 0


def test_multi_marker_mixing():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, markers={"X": 2.0, "Y": 8.0}),
            SampleIn(id="B", initial_volume=10.0, markers={"X": 6.0}),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=10.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    b = result["steps"][0]["target_after"]
    # B：X 质量 60 + 20 = 80；Y 质量 80；体积 20
    assert b["concentrations"]["X"] == 4.0
    assert b["concentrations"]["Y"] == 4.0
    # A 已空，所有标记物浓度按 0 返回
    a = result["steps"][0]["source_after"]
    assert a["volume"] == 0
    assert a["concentrations"] == {"X": 0.0, "Y": 0.0}
    assert a["origins"] == []


def test_full_transfer_conservation():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=5.0, concentration=3.0)],
        transfers=[],  # 空操作序列也合法
    )
    result = run_simulation(request)
    assert result["status"] == "success"
    assert result["steps"] == []
    assert result["conservation"]["volume_difference"] == 0


def test_insufficient_volume_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="B", amount=11.0)],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    error = result["error"]
    assert error["code"] == "INSUFFICIENT_VOLUME"
    assert error["transfer_index"] == 0
    assert error["location"] == "transfers[0].amount"
    # 失败时不产生步骤，状态停留在初始
    assert result["steps"] == []


def test_unknown_source_error_location():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="C", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].source"


def test_unknown_target_error_location():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=5.0, concentration=0.0),
        ],
        transfers=[TransferIn(time=1, source="A", target="C", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "UNKNOWN_SAMPLE"
    assert result["error"]["location"] == "transfers[0].target"


def test_time_reversal_error():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=2, source="A", target="B", amount=1.0),
            TransferIn(time=1, source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIME_NOT_ORDERED"
    assert result["error"]["transfer_index"] == 1
    assert result["error"]["location"] == "transfers[1].time"
    # 第一步成功保留
    assert len(result["steps"]) == 1


def test_iso_time_ordering_supported():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time="2026-09-18T10:00:00Z", source="A", target="B", amount=1.0),
            TransferIn(time="2026-09-18T11:00:00Z", source="A", target="B", amount=1.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"


def test_same_source_target_rejected():
    request = SimulationRequest(
        samples=[SampleIn(id="A", initial_volume=10.0, concentration=1.0)],
        transfers=[TransferIn(time=1, source="A", target="A", amount=1.0)],
    )
    result = run_simulation(request)
    assert result["error"]["code"] == "SAME_SOURCE_TARGET"


# ---------- 移液损耗（delivered_amount / waste）追踪 ----------


def test_pipetting_loss_split_and_waste():
    # A(100, c=10) 吸取 40 但只注入 30：B 收到 30，损耗 10（浓度 10）
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=40.0, delivered_amount=30.0
            ),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"
    step = result["steps"][0]

    # 请求量与实际到账量分别返回
    assert step["amount"] == 40.0
    assert step["delivered_amount"] == 30.0

    # 源管按吸取量 40 扣减，目标管只增加 delivered 30
    assert step["source_after"]["volume"] == 60.0
    assert step["target_after"]["volume"] == 130.0
    # B：质量 300 / 体积 130
    assert abs(step["target_after"]["concentrations"]["default"] - 300 / 130) < 1e-9

    # 本步损耗：10 体积、浓度 10、质量 100、全部来自 A
    lost = step["lost"]
    assert lost["volume"] == 10.0
    assert lost["concentrations"]["default"] == 10.0
    assert lost["marker_amount"]["default"] == 100.0
    assert lost["origins"] == [
        {"source_id": "A", "volume": 10.0, "volume_fraction": 1.0}
    ]

    # 累计废液与本步损耗一致
    waste = result["waste"]
    assert waste["volume"] == 10.0
    assert waste["marker_amount"]["default"] == 100.0
    assert waste["concentrations"]["default"] == 10.0
    assert waste["origins"][0]["source_id"] == "A"

    cons = result["conservation"]
    # 管内口径：100+100=200 -> 60+130=190，管内少了 10
    assert cons["initial_total_volume"] == 200.0
    assert cons["final_total_volume"] == 190.0
    assert cons["volume_difference"] == -10.0
    # 系统口径：管内 + 废液，守恒
    assert cons["waste_total_volume"] == 10.0
    assert cons["system_initial_volume"] == 200.0
    assert cons["system_final_volume"] == 200.0
    assert cons["system_volume_difference"] == 0.0
    # 标记物：管内质量 600 + 300 = 900，废液 100，合计仍为 1000
    assert cons["final_marker_amount"]["default"] == 900.0
    assert cons["waste_marker_amount"]["default"] == 100.0
    assert cons["system_final_marker_amount"]["default"] == 1000.0
    assert cons["system_marker_amount_difference"]["default"] == 0.0

    # 逐管体积变化
    changes = {c["tube_id"]: c for c in cons["tube_volume_changes"]}
    assert changes["A"]["volume_change"] == -40.0
    assert changes["B"]["volume_change"] == 30.0


def test_loss_step_conservation_and_volume_changes():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=40.0, delivered_amount=30.0
            ),
        ],
    )
    result = run_simulation(request)
    c = result["steps"][0]["conservation"]

    # 单步管内口径
    assert c["tube_volume_before"] == 200.0
    assert c["tube_volume_after"] == 190.0
    assert c["tube_volume_change"] == -10.0
    assert c["tube_marker_amount_change"]["default"] == -100.0
    # 废液前后
    assert c["waste_volume_before"] == 0.0
    assert c["waste_volume_after"] == 10.0
    assert c["waste_marker_amount_after"]["default"] == 100.0
    # 系统口径差值为 0
    assert c["system_volume_difference"] == 0.0
    assert c["system_marker_amount_difference"]["default"] == 0.0
    # 该步逐管体积变化
    per_tube = {x["tube_id"]: x for x in c["tube_volume_changes"]}
    assert per_tube["A"]["volume_before"] == 100.0
    assert per_tube["A"]["volume_after"] == 60.0
    assert per_tube["A"]["volume_change"] == -40.0
    assert per_tube["B"]["volume_change"] == 30.0


def test_waste_accumulates_over_multiple_steps_with_mixed_origins():
    # 第一步：A(100,c=10) 吸 40 到 30 给 B（B: 130, 质量 300），损耗 10 纯 A
    # 第二步：B 吸 70 到 60 给 A。B 中 A:B = 30:100，
    #   吸取 70 => A 来源 70*30/130，B 来源 70*100/130；
    #   损耗 10 按同比例：A 来源 30/13，B 来源 100/13
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=100.0, concentration=10.0),
            SampleIn(id="B", initial_volume=100.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=40.0, delivered_amount=30.0),
            TransferIn(time=2, source="B", target="A", amount=70.0, delivered_amount=60.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"

    waste = result["waste"]
    assert abs(waste["volume"] - 20.0) < 1e-9
    # 废液来源：A 10 + 30/13；B 100/13
    w_origins = {o["source_id"]: o for o in waste["origins"]}
    assert abs(w_origins["A"]["volume"] - (10.0 + 30.0 / 13.0)) < 1e-9
    assert abs(w_origins["B"]["volume"] - 100.0 / 13.0) < 1e-9
    assert abs(sum(o["volume_fraction"] for o in waste["origins"]) - 1.0) < 1e-9

    # 每步及全程系统口径守恒
    for step in result["steps"]:
        assert step["conservation"]["system_volume_difference"] == 0
        assert step["conservation"]["system_marker_amount_difference"]["default"] == 0
    cons = result["conservation"]
    assert cons["system_volume_difference"] == 0
    assert abs(cons["system_final_marker_amount"]["default"] - 1000.0) < 1e-9
    assert abs(cons["system_marker_amount_difference"]["default"]) < 1e-9
    # 管内总体积 = 200 - 20
    assert cons["final_total_volume"] == 180.0
    # 第二步损耗液浓度 = B 当时浓度 300/130
    lost2 = result["steps"][1]["lost"]
    assert abs(lost2["concentrations"]["default"] - 300 / 130) < 1e-9
    assert abs(lost2["marker_amount"]["default"] - 10.0 * 300 / 130) < 1e-9


def test_loss_with_multiple_markers():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, markers={"X": 2.0, "Y": 8.0}),
            SampleIn(id="B", initial_volume=10.0, markers={"X": 6.0}),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=4.0, delivered_amount=3.0),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "success"
    waste = result["waste"]
    # 损耗 4-3=1，携带 A 的浓度 X=2, Y=8
    assert waste["volume"] == 1.0
    assert abs(waste["marker_amount"]["X"] - 2.0) < 1e-9
    assert abs(waste["marker_amount"]["Y"] - 8.0) < 1e-9
    cons = result["conservation"]
    assert cons["system_marker_amount_difference"]["X"] == 0
    assert cons["system_marker_amount_difference"]["Y"] == 0
    # 管内：B 收到 3 体积，X 质量 60 + 6 = 66；Y 质量 24
    final_b = next(t for t in result["final_states"] if t["id"] == "B")
    assert final_b["volume"] == 13.0
    assert abs(final_b["concentrations"]["X"] - 66 / 13) < 1e-9
    assert abs(final_b["concentrations"]["Y"] - 24 / 13) < 1e-9


def test_delivered_equal_amount_means_no_loss_fields_zero():
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(time=1, source="A", target="B", amount=4.0, delivered_amount=4.0),
        ],
    )
    result = run_simulation(request)
    step = result["steps"][0]
    assert step["delivered_amount"] == 4.0
    assert step["lost"]["volume"] == 0.0
    assert step["lost"]["origins"] == []
    assert result["waste"]["volume"] == 0.0
    assert result["conservation"]["system_volume_difference"] == 0.0


def test_overdraw_still_validated_against_amount_not_delivered():
    # 源管只有 10：amount=11 即使 delivered_amount 很小也直接超量失败
    request = SimulationRequest(
        samples=[
            SampleIn(id="A", initial_volume=10.0, concentration=1.0),
            SampleIn(id="B", initial_volume=10.0, concentration=0.0),
        ],
        transfers=[
            TransferIn(
                time=1, source="A", target="B", amount=11.0, delivered_amount=1.0
            ),
        ],
    )
    result = run_simulation(request)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INSUFFICIENT_VOLUME"
    assert result["error"]["location"] == "transfers[0].amount"
    assert result["steps"] == []


def test_delivered_amount_validation():
    import pytest
    from pydantic import ValidationError

    # delivered_amount > amount => 422 层的校验错误
    with pytest.raises(ValidationError):
        TransferIn(
            time=1, source="A", target="B",
            amount=10.0, delivered_amount=10.5,
        )
    # delivered_amount <= 0 同样非法
    with pytest.raises(ValidationError):
        TransferIn(
            time=1, source="A", target="B",
            amount=10.0, delivered_amount=0.0,
        )
