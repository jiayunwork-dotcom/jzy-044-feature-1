"""夹点判定与非法输入（内核层）。"""

from __future__ import annotations

import pytest

from app import config, engine
from app.errors import CalculationError


BASE = dict(G=1.0, L=1.6, Kya=1.0, a=1.0, S=1.0,
            y1=0.10, y2=0.02, x1=0.05, x2=0.0, m=1.0)


def expect_error(params, code):
    with pytest.raises(CalculationError) as ei:
        engine.run_calculation(params)
    assert ei.value.code == code
    return ei.value


class TestPinch:
    def test_exact_pinch_at_bottom_is_rejected(self):
        """L/G=(L/G)_min=0.8，操作线在塔底与平衡线相交，Δ1=0。"""
        p = dict(BASE, L=0.8, x1=0.10)
        exc = expect_error(p, "PINCH_LIMITED")
        assert "提高液气比" in exc.message or "增大 L/G" in exc.message

    def test_driving_force_negative_somewhere_is_rejected(self):
        """L/G 低于最小值：操作线与平衡线交叉，端点推动力为负。"""
        p = dict(BASE, L=0.5, x1=0.16)  # 衡算：0.5·0.16=0.08 自洽；Δ1=-0.06
        expect_error(p, "PINCH_LIMITED")

    def test_tangency_within_tolerance_is_rejected(self):
        """Δ1 极小但为正（数值相切）：按夹点拒绝，不返回巨大但虚假的塔高。"""
        tiny = config.PINCH_ATOL / 2
        # 构造 Δ1=tiny：x1=(y1-tiny)/m；用 L 把衡算配平
        x1 = BASE["y1"] - tiny
        L = BASE["G"] * (BASE["y1"] - BASE["y2"]) / (x1 - BASE["x2"])
        p = dict(BASE, L=L, x1=x1)
        expect_error(p, "PINCH_LIMITED")

    def test_near_pinch_but_valid_reports_distance(self):
        """略高于最小液气比：可算，但结果必须显式说明接近夹点。"""
        # L/G 比最小值高 0.1%
        L = 0.8 * 1.001
        x1 = (BASE["y1"] - BASE["y2"]) / L  # x2=0
        p = dict(BASE, L=L, x1=x1)
        r = engine.run_calculation(p)
        assert r["pinch_limited"] is False
        assert r["min_driving_force"] < 1e-4
        assert r["distance_to_pinch"]["L_over_G_excess_pct"] == pytest.approx(0.1, rel=1e-3)

    def test_lambda_one_with_zero_force_is_pinch(self):
        """λ=1 且平行相切（推动力恒为零）同样按夹点拒绝。"""
        p = dict(G=1.0, L=2.0, Kya=1.0, a=1.0, S=1.0,
                 y1=0.10, y2=0.04, x1=0.05, x2=0.02, m=2.0)
        # 衡算：1·0.06=2·0.03；Δ1=0.10-0.10=0，Δ2=0.04-0.04=0
        expect_error(p, "PINCH_LIMITED")

    def test_m_zero_never_pinch(self):
        """m=0：平衡分率恒为零，不存在有限最小液气比。"""
        p = dict(BASE, m=0.0, x1=0.05)
        r = engine.run_calculation(p)
        assert r["minimum_L_over_G"] is None
        assert r["L_over_G_excess_pct"] is None
        assert r["pinch_limited"] is False
        # y* 恒 0 -> NOG = ln(y1/y2) = ln 5
        import math
        assert r["NOG"] == pytest.approx(math.log(5.0), rel=1e-9)


class TestInvalidInputs:
    @pytest.mark.parametrize("field,bad", [
        ("G", 0.0), ("G", -1.0),
        ("L", 0.0), ("L", -2.0),
        ("Kya", 0.0), ("a", -0.5), ("S", 0.0),
    ])
    def test_non_positive_flows_and_coefficients(self, field, bad):
        expect_error(dict(BASE, **{field: bad}), "NON_POSITIVE_VALUE")

    @pytest.mark.parametrize("field,bad", [
        ("y1", 1.01), ("y2", -0.01), ("x1", 2.0), ("x2", -1e-9),
    ])
    def test_mole_fractions_out_of_range(self, field, bad):
        expect_error(dict(BASE, **{field: bad}), "FRACTION_OUT_OF_RANGE")

    def test_negative_slope_rejected(self):
        expect_error(dict(BASE, m=-0.1), "INVALID_SLOPE")

    def test_y1_below_y2_rejected(self):
        expect_error(dict(BASE, y1=0.02, y2=0.10, x1=0.0, x2=0.05), "Y_ORDER_INVALID")

    def test_mass_balance_contradiction_rejected(self):
        exc = expect_error(dict(BASE, x1=0.09), "MASS_BALANCE_MISMATCH")
        assert "物料衡算" in exc.message

    @pytest.mark.parametrize("drop", list(BASE.keys()))
    def test_missing_field_rejected(self, drop):
        p = dict(BASE)
        del p[drop]
        expect_error(p, "MISSING_FIELD")

    def test_nan_and_infinity_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CalculationError) as ei:
                engine.run_calculation(dict(BASE, G=bad))
            assert ei.value.code == "NOT_A_NUMBER"

    def test_boolean_rejected_as_number(self):
        with pytest.raises(CalculationError) as ei:
            engine.run_calculation(dict(BASE, G=True))  # type: ignore[arg-type]
        assert ei.value.code == "NOT_A_NUMBER"

    def test_boundary_fractions_zero_and_one_are_legal(self):
        """分率边界 0 与 1 本身合法（y1=1, y2=0.2, m=0 规避 y2=0 的固有夹点）。"""
        p = dict(BASE, m=0.0, y1=1.0, y2=0.2, x2=0.0,
                 x1=BASE["G"] * (1.0 - 0.2) / BASE["L"])
        r = engine.run_calculation(p)
        assert r["pinch_limited"] is False
