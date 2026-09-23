"""反解内核：示范工况手算、正反算自洽、单调性、夹点可行性边界、求根收敛。

直接打纯函数 :func:`app.invert.run_inversion`，不依赖 HTTP，便于精算核对。
"""

from __future__ import annotations

import math

import pytest

from app import engine, invert
from app.errors import CalculationError

# 与内置示范工况 air_water_demo 同一底座（L / y2 / x1 在反解中不是底座）
BASE = dict(G=1.0, Kya=1.0, a=1.0, S=1.0, y1=0.10, x2=0.0, m=1.0)
ZDEMO = 2.4434419516644135  # 示范工况正算高度：L/G=1.6, y2=0.02


def inv_error(raw, code):
    with pytest.raises(CalculationError) as ei:
        invert.run_inversion(raw)
    assert ei.value.code == code, f"期望 {code}，实得 {ei.value.code}: {ei.value.message}"
    return ei.value


class TestDemoHandCalc:
    """对内置可手算示范工况反解，结果落在手工可验证范围内。"""

    def test_fixed_height_of_demo_recovers_L(self):
        """钉死示范塔高与目标 y2=0.02，反解必须还回 L/G=1.6、x1=0.05。"""
        r = invert.run_inversion({**BASE, "target_y2": 0.02, "Z": ZDEMO})
        assert r["solution"]["L_over_G"] == pytest.approx(1.6, rel=1e-8)
        assert r["solution"]["L"] == pytest.approx(1.6, rel=1e-8)
        assert r["solution"]["x1"] == pytest.approx(0.05, rel=1e-8)
        assert r["solver"]["converged"] is True
        assert r["solver"]["method"] == "bisection"

    def test_design_factor_two_recovers_demo(self):
        """示范工况即 (L/G)_min=0.8 的 2 倍选型：80% 脱除 + c=2 -> L/G=1.6。"""
        r = invert.run_inversion({**BASE, "removal_pct": 80.0, "design_factor": 2.0})
        assert r["solution"]["L_over_G"] == pytest.approx(1.6, abs=1e-12)
        assert r["solution"]["x1"] == pytest.approx(0.05, abs=1e-12)
        assert r["limits"]["minimum_L_over_G"] == pytest.approx(0.8)

    def test_removal_percent_equivalent_to_target_y2(self):
        a = invert.run_inversion({**BASE, "removal_pct": 80.0, "design_factor": 2.0})
        b = invert.run_inversion({**BASE, "target_y2": 0.02, "design_factor": 2.0})
        assert a["solution"]["L_over_G"] == pytest.approx(b["solution"]["L_over_G"], abs=1e-12)
        assert a["target"]["y2"] == pytest.approx(0.02, abs=1e-14)
        assert a["target"]["removal_pct"] == pytest.approx(80.0)


class TestForwardInverseConsistency:
    """解点必须用正算内核复算，且与目标/固定量在容差内吻合（自洽性）。"""

    @pytest.mark.parametrize("y2,Z", [
        (0.05, 1.3),
        (0.03, 2.2),
        (0.02, ZDEMO),
        (0.01, 3.0),
        (0.005, 5.0),
    ])
    def test_recomputed_height_matches_fixed_Z(self, y2, Z):
        r = invert.run_inversion({**BASE, "target_y2": y2, "Z": Z})
        v = r["verification"]
        # 正算复算的入口分率就是目标，塔高与给定可用高度吻合
        assert v["inputs"]["y2"] == pytest.approx(y2)
        assert v["Z"] == pytest.approx(Z, rel=1e-8, abs=1e-9)
        assert v["pinch_limited"] is False
        # 衡算自洽
        assert v["inputs"]["x1"] == pytest.approx(
            BASE["x2"] + (BASE["y1"] - y2) / r["solution"]["L_over_G"]
        )

    def test_recomputed_result_is_full_forward_output(self):
        r = invert.run_inversion({**BASE, "target_y2": 0.02, "Z": ZDEMO})
        # 复算结果就是正算内核在解点参数上的完整输出，逐字段一致
        direct = engine.run_calculation({
            **BASE, "L": r["solution"]["L"], "y2": 0.02, "x1": r["solution"]["x1"]
        })
        for key in ("HOG", "NOG", "Z", "driving_force_bottom", "driving_force_top",
                    "minimum_L_over_G", "slope_ratio"):
            assert r["verification"][key] == direct[key]

    def test_design_factor_solution_recomputes_its_height(self):
        r = invert.run_inversion({**BASE, "target_y2": 0.02, "design_factor": 1.5})
        lg = 1.5 * 0.8
        assert r["solution"]["L_over_G"] == pytest.approx(lg)
        assert r["verification"]["Z"] == pytest.approx(r["verification"]["HOG"] * r["verification"]["NOG"])

    def test_explicit_L_recomputes_required_height(self):
        r = invert.run_inversion({**BASE, "target_y2": 0.02, "L_over_G": 2.0})
        assert r["solver"]["method"] == "direct"
        x1 = (0.10 - 0.02) / 2.0
        assert r["solution"]["x1"] == pytest.approx(x1)
        # 手算：Δ1=0.10-0.04=0.06, Δ2=0.02, Δlm=0.04/ln3=0.03641
        delta_lm = 0.04 / math.log(3.0)
        assert r["verification"]["NOG"] == pytest.approx(0.08 / delta_lm, rel=1e-9)


class TestMonotonicity:
    """目标每苛刻一档：所需 L 单调变大（定塔高），可行塔高单调变高（定倍率）。"""

    TARGETS = (0.06, 0.04, 0.03, 0.02, 0.015, 0.01)

    def test_required_L_increases_as_target_tightens(self):
        lgs = [
            invert.run_inversion({**BASE, "target_y2": y2, "Z": 3.0})["solution"]["L_over_G"]
            for y2 in self.TARGETS
        ]
        assert all(lo < hi for lo, hi in zip(lgs, lgs[1:]))
        # 逼近无穷溶剂极限时 L/G 发散：最小与最大差出量级
        assert lgs[-1] > 5 * lgs[0]

    def test_required_height_increases_as_target_tightens(self):
        zs = [
            invert.run_inversion({**BASE, "target_y2": y2, "design_factor": 1.5})
            ["verification"]["Z"]
            for y2 in self.TARGETS
        ]
        assert all(lo < hi for lo, hi in zip(zs, zs[1:]))
        # 全部高于各自的理论最小塔高（有限 L 永远省不到极限）
        for y2, z in zip(self.TARGETS, zs):
            assert z > math.log(0.10 / y2)  # HOG=1, x2=0, m=1

    def test_taller_tower_allows_deeper_outlet(self):
        """塔越高，定塔高反解给出的最好出口分率越低（越干净）。"""
        outs = []
        for zmax in (2.0, 2.5, 3.0, 4.0):
            r = invert.run_inversion(
                {**BASE, "mode": "fixed_height", "Z_max": zmax, "limit_band": 0.01}
            )
            outs.append(r["solution"]["y2"])
        assert all(lo > hi for lo, hi in zip(outs, outs[1:]))


class TestFeasibilityBoundary:
    """不可行目标必须稳定报错，报出缺口，而不是吐夹点大数或不收敛。"""

    def test_outlet_below_equilibrium_floor_unreachable(self):
        # mx2=0，y2=0 即 100% 脱除，只有 L→∞ 才可能
        exc = inv_error({**BASE, "target_y2": 0.0, "Z": 3.0}, "TARGET_UNREACHABLE")
        assert "极限" in exc.message

    def test_target_within_pinch_tolerance_band_unreachable(self):
        # 夹点带：y2 ≤ mx2 + max(1e-10,1e-9·y1)=1e-10
        inv_error({**BASE, "target_y2": 5e-11, "Z": 100.0}, "TARGET_UNREACHABLE")
        # 带外一点点仍可行（塔足够高时）
        r = invert.run_inversion({**BASE, "target_y2": 1e-8, "Z": 30.0})
        assert r["verification"]["inputs"]["y2"] == pytest.approx(1e-8)

    def test_height_below_minimum_reports_gap(self):
        # y2=0.02 的理论最小塔高 Z_min=ln5≈1.6094（无穷吸收剂极限）
        z_min = math.log(5.0)
        exc = inv_error({**BASE, "target_y2": 0.02, "Z": 1.5}, "TARGET_UNREACHABLE")
        assert "Z_min" in exc.message
        assert z_min - 1.5 == pytest.approx(0.10944, abs=1e-4)

    def test_height_exactly_at_minimum_is_infeasible_by_pinch_rule(self):
        """恰好落在理论最小塔高上：只有 L/G→∞ 可逼近，按夹点口径判不可行。"""
        z_min = math.log(5.0)
        inv_error({**BASE, "target_y2": 0.02, "Z": z_min}, "TARGET_UNREACHABLE")

    def test_fixed_height_explicit_target_too_deep(self):
        exc = inv_error(
            {**BASE, "mode": "fixed_height", "Z_max": ZDEMO, "target_y2": 0.005},
            "TARGET_UNREACHABLE",
        )
        # Z_min(0.005)=ln20≈2.996 > 2.443
        assert "2.99" in exc.message or "Z_min" in exc.message

    def test_absorbent_with_no_capacity_rejected(self):
        # mx2 ≥ y1：贫液平衡分压不低于进气，无推动力
        inv_error({**BASE, "x2": 0.1, "target_y2": 0.05, "Z": 3.0}, "TARGET_UNREACHABLE")

    def test_x2_equal_one_rejected(self):
        inv_error({**BASE, "x2": 1.0, "target_y2": 0.05, "Z": 3.0}, "TARGET_UNREACHABLE")

    def test_design_factor_below_one_pinch_rejected(self):
        # c=1 即 L/G=(L/G)_min，正算内核判夹点
        inv_error({**BASE, "target_y2": 0.02, "design_factor": 1.0}, "PINCH_LIMITED")
        inv_error({**BASE, "target_y2": 0.02, "design_factor": 0.5}, "PINCH_LIMITED")

    def test_loose_target_returns_small_solution_not_padded(self):
        """目标松到极小液气比即可满足时，老实返回贴下界的小解，不加码。"""
        # y2=0.099 只脱 1%；给一个远大于所需的塔，解取最小可行液气比边界
        r = invert.run_inversion({**BASE, "target_y2": 0.099, "Z": 1e4})
        assert r["solver"]["method"] == "boundary_minimum_lg"
        lg_min = 1.0 * (0.10 - 0.099) / 0.10  # 纯溶剂夹点侧最小液气比 0.01
        assert r["solution"]["L_over_G"] == pytest.approx(lg_min, rel=1e-6)


class TestFixedHeight:
    def test_auto_optimum_recomputes_to_full_height(self):
        r = invert.run_inversion({**BASE, "mode": "fixed_height", "Z_max": ZDEMO})
        assert r["fixed"]["Z_used"] == pytest.approx(ZDEMO, rel=1e-8)
        assert abs(r["fixed"]["height_slack"]) < 1e-8
        # 报告无穷吸收剂极限与本解的差距
        y_lim = r["fixed"]["y2_best_infinite_solvent_limit"]
        assert y_lim == pytest.approx(0.10 * math.exp(-ZDEMO), rel=1e-9)
        assert r["solution"]["y2"] > y_lim  # 有限 L 必然略差于极限

    def test_explicit_target_feasible_uses_full_height(self):
        r = invert.run_inversion(
            {**BASE, "mode": "fixed_height", "Z_max": ZDEMO, "target_y2": 0.02}
        )
        assert r["solution"]["L_over_G"] == pytest.approx(1.6, rel=1e-8)
        assert r["fixed"]["Z_used"] == pytest.approx(ZDEMO, rel=1e-8)

    def test_limit_band_smaller_approaches_limit_with_larger_L(self):
        bands = (0.05, 0.01, 0.002)
        ys, lgs = [], []
        for b in bands:
            r = invert.run_inversion(
                {**BASE, "mode": "fixed_height", "Z_max": ZDEMO, "limit_band": b}
            )
            ys.append(r["solution"]["y2"])
            lgs.append(r["solution"]["L_over_G"])
        assert all(a > b for a, b in zip(ys, ys[1:]))       # 带越窄出口越净
        assert all(a < b for a, b in zip(lgs, lgs[1:]))     # 带越窄 L 越大（发散）


class TestDegenerateAndEdgeCases:
    def test_zero_removal_is_degenerate(self):
        r = invert.run_inversion({**BASE, "target_y2": 0.10, "Z": 2.0})
        assert r["degenerate"] is True
        assert r["verification"]["NOG"] == 0.0
        assert r["verification"]["Z"] == 0.0
        assert r["warnings"]  # 说明 L 不唯一、取地板

    def test_zero_removal_respects_explicit_L(self):
        r = invert.run_inversion({**BASE, "removal_pct": 0.0, "L_over_G": 1.6})
        assert r["degenerate"] is True
        assert r["solution"]["L_over_G"] == pytest.approx(1.6)

    def test_m_zero_floor_is_zero(self):
        b = dict(BASE, m=0.0)
        r = invert.run_inversion({**b, "target_y2": 0.02, "Z": math.log(5.0) * 1.01})
        assert r["limits"]["infinite_solvent_y2"] == 0.0
        assert r["verification"]["pinch_limited"] is False
        # m=0 不存在夹点侧最小液气比
        assert r["limits"]["minimum_L_over_G"] == pytest.approx(0.08)  # 仅 x1≤1 侧

    def test_x1_fraction_bound_can_dominate_pinch_bound(self):
        """高浓度进气：x1≤1 的分数侧约束可能比夹点侧更紧，下界必须取大。"""
        # y1=0.5, m=0.1, x2=0, y2=0.05：夹点侧 0.09，分数侧 0.45
        b = dict(BASE, y1=0.5, m=0.1)
        r = invert.run_inversion({**b, "target_y2": 0.05, "Z": 1e5})
        assert r["limits"]["minimum_L_over_G"] == pytest.approx(0.45)
        assert r["solver"]["method"] == "boundary_minimum_lg"
        assert r["solution"]["x1"] <= 1.0
        # 只按夹点侧 2 倍选型（lg=0.18）会逼出 x1=2.5，正算内核必须拒绝
        inv_error({**b, "target_y2": 0.05, "design_factor": 2.0}, "FRACTION_OUT_OF_RANGE")

    def test_fixed_height_deep_removal_x1_bound_respected(self):
        b = dict(BASE, y1=0.5, m=0.1)
        r = invert.run_inversion(
            {**b, "mode": "fixed_height", "Z_max": 2.5, "target_y2": 0.05}
        )
        assert r["solution"]["x1"] <= 1.0 + 1e-9
        assert r["verification"]["pinch_limited"] is False


class TestSpecValidation:
    def test_target_required(self):
        inv_error({**BASE, "Z": 3.0}, "INVALID_TARGET")

    def test_two_target_writings_rejected(self):
        inv_error({**BASE, "target_y2": 0.02, "removal_pct": 80, "Z": 3.0}, "INVALID_TARGET")

    def test_target_out_of_range(self):
        inv_error({**BASE, "target_y2": 1.2, "Z": 3.0}, "INVALID_TARGET")

    def test_target_above_inlet_rejected(self):
        inv_error({**BASE, "target_y2": 0.2, "Z": 3.0}, "INVALID_TARGET")

    def test_two_controls_rejected(self):
        inv_error(
            {**BASE, "target_y2": 0.02, "Z": 3.0, "design_factor": 2.0},
            "INVERT_SPEC_INVALID",
        )

    def test_no_control_under_determined(self):
        inv_error({**BASE, "target_y2": 0.02}, "INVERT_SPEC_INVALID")

    def test_base_forbidden_fields(self):
        for name in ("y2", "x1"):
            inv_error({**BASE, name: 0.02, "Z": 3.0}, "INVERT_SPEC_INVALID")

    def test_fixed_height_controls_forbidden(self):
        for name in ("Z", "design_factor", "L", "L_over_G"):
            inv_error(
                {**BASE, "mode": "fixed_height", "Z_max": 3.0, name: 1.0},
                "INVERT_SPEC_INVALID",
            )

    def test_fixed_height_requires_Z_max(self):
        inv_error(
            {**BASE, "mode": "fixed_height", "target_y2": 0.02}, "INVERT_SPEC_INVALID"
        )

    def test_zmax_wrong_mode(self):
        inv_error(
            {**BASE, "target_y2": 0.02, "Z_max": 3.0}, "INVERT_SPEC_INVALID"
        )

    def test_unknown_mode(self):
        inv_error({**BASE, "mode": "sideways", "target_y2": 0.02, "Z": 3.0},
                  "INVERT_SPEC_INVALID")

    def test_design_factor_undefined_when_m_zero(self):
        inv_error(
            {**BASE, "m": 0.0, "target_y2": 0.02, "design_factor": 2.0},
            "INVERT_SPEC_INVALID",
        )

    @pytest.mark.parametrize("field,bad", [
        ("G", 0.0), ("Kya", -1.0), ("a", 0.0), ("S", -2.0),
        ("y1", 1.1), ("x2", -0.1), ("m", -0.5),
        ("Z", 0.0), ("Z_max", -1.0), ("xtol", 0.0),
    ])
    def test_bad_numbers_rejected(self, field, bad):
        raw = {**BASE, "target_y2": 0.02, "Z": 3.0, field: bad}
        with pytest.raises(CalculationError):
            invert.run_inversion(raw)


class TestSolverConvergence:
    def test_tight_xtol_achieved_with_enough_iterations(self):
        r = invert.run_inversion(
            {**BASE, "target_y2": 0.001, "Z": 8.0, "xtol": 1e-13, "max_iterations": 100}
        )
        assert r["solver"]["converged"] is True
        lo, hi = r["solver"]["bracket"]
        assert (hi - lo) / hi <= 1e-13
        assert r["verification"]["Z"] == pytest.approx(8.0, rel=1e-10)

    def test_iteration_cap_reported_honestly(self):
        exc = inv_error(
            {**BASE, "target_y2": 0.02, "Z": ZDEMO, "xtol": 1e-16, "max_iterations": 5},
            "SOLVER_DID_NOT_CONVERGE",
        )
        assert "5" in exc.message
