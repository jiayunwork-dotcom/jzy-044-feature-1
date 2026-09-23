"""反解内核：自洽性、单调性、可行性边界与迭代收敛。

这些用例直接打 :mod:`app.inverse` 纯函数内核，不依赖 HTTP，便于精算核对。
两条核心诉求各覆盖：

* ``flow_for_target``  定分离、求吸收剂用量；
* ``outlet_for_height`` 定塔高、求可行最优操作（自由流量 / 固定流量）。
"""

from __future__ import annotations

import math

import pytest

from app import config, engine, inverse
from app.errors import CalculationError

# 与内置示范工况同底座：G=Kya=a=S=1, y1=0.10, x2=0, m=1 -> HOG=1
BASE = dict(G=1.0, Kya=1.0, a=1.0, S=1.0, y1=0.10, x2=0.0, m=1.0)
Z_DEMO = 2.4434419516644135   # 示范工况正算塔高（L=1.6, y2=0.02）


@pytest.fixture
def settings():
    return dict(
        x_tol_rel=1e-11, x_tol_abs=1e-12, max_iter=100.0,
        q_floor=1e-9, q_ceil=1e12,
    )


def expect_infeasible(fn):
    with pytest.raises(CalculationError) as ei:
        fn()
    assert ei.value.code == "TARGET_INFEASIBLE"
    return ei.value


# ------------------------------------------------------- 示范工况：手工可核对


class TestDemoHandCalcInverse:
    def test_flow_for_target_recovers_demo_L(self, settings):
        """80% 脱除（y2=0.02）、给示范塔高 -> 反解 L 必须回到手算的 1.6。"""
        r = inverse.solve_flow_for_target(BASE, 0.02, Z_DEMO, settings)
        assert r["solution"]["L_over_G"] == pytest.approx(1.6, abs=1e-8)
        assert r["solution"]["L"] == pytest.approx(1.6, abs=1e-8)
        assert r["solution"]["x1"] == pytest.approx(0.05, abs=1e-9)
        assert r["solver"]["converged"] is True
        # 正算复算自洽
        fwd = r["verification"]["forward_calculation"]
        assert fwd["NOG"] == pytest.approx(2.4434419517, rel=1e-9)
        assert fwd["Z"] == pytest.approx(Z_DEMO, rel=1e-10)
        assert fwd["driving_force_bottom"] == pytest.approx(0.05)
        assert fwd["driving_force_top"] == pytest.approx(0.02)
        assert r["verification"]["recomputed_y2"] == pytest.approx(0.02, abs=1e-12)
        assert abs(r["verification"]["mass_balance_residual"]) < 1e-12
        # 理论边界：Z_min = HOG·ln(y1/y2) = ln 5
        assert r["feasibility_limits"]["z_min_at_target"] == pytest.approx(math.log(5.0))
        assert r["feasibility_limits"]["minimum_L_over_G"] == pytest.approx(0.8)

    def test_flow_for_target_with_other_height_matches_hand_formula(self, settings):
        """给定 z_available=3 反解 L，再用吸收因子封闭式手算核对。"""
        r = inverse.solve_flow_for_target(BASE, 0.02, 3.0, settings)
        q = r["solution"]["L_over_G"]
        lam = 1.0 / q
        # NOG = 1/(1-λ)·ln[(1-λ)·(y1)/(y2)+λ] = 3（HOG=1）
        nog = math.log((1 - lam) * 0.10 / 0.02 + lam) / (1 - lam)
        assert nog == pytest.approx(3.0, rel=1e-8)
        # 塔越富余，所需吸收剂越少：q < 1.6
        assert q < 1.6
        assert r["verification"]["forward_calculation"]["Z"] == pytest.approx(3.0, rel=1e-8)

    def test_outlet_for_height_free_recovers_demo_outlet_at_fixed_L(self, settings):
        """定塔高=示范塔高且固定 L=1.6 -> 最优 y2 必须回到 0.02。"""
        r = inverse.solve_outlet_for_height(
            BASE, Z_DEMO, settings, fixed_q=1.6, optimality_gap_pct=1.0
        )
        assert r["theoretical_best"]["y2"] == pytest.approx(0.02, abs=1e-8)
        fwd = r["verification"]["forward_calculation"]
        assert fwd["Z"] == pytest.approx(Z_DEMO, rel=1e-9)

    def test_outlet_for_height_free_matches_exponential_hand_calc(self, settings):
        """自由流量：y2_best = y1·exp(-N)（L→∞, λ→0），可手算。"""
        r = inverse.solve_outlet_for_height(
            BASE, Z_DEMO, settings, fixed_q=None, optimality_gap_pct=1.0
        )
        n = Z_DEMO  # HOG=1
        assert r["theoretical_best"]["y2"] == pytest.approx(0.10 * math.exp(-n), rel=1e-10)
        assert r["theoretical_best"]["L_over_G"] == math.inf
        # 实操点比理论最优松 1%
        assert r["practical"]["y2"] == pytest.approx(
            r["theoretical_best"]["y2"] * 1.01, rel=1e-8
        )
        fwd = r["verification"]["forward_calculation"]
        assert fwd["Z"] <= Z_DEMO * (1 + 1e-9)   # 不超过塔高上限
        assert fwd["Z"] == pytest.approx(Z_DEMO, rel=1e-6)


# ------------------------------------------------------- 自洽性：反解必被正算复核


class TestForwardConsistency:
    @pytest.mark.parametrize("y2,z", [
        (0.02, 2.4434419516644135),
        (0.01, 4.0),
        (0.05, 1.2),
        (0.005, 5.0),
    ])
    def test_flow_solution_recomputed_by_forward_matches_target(self, settings, y2, z):
        r = inverse.solve_flow_for_target(BASE, y2, z, settings)
        fwd = r["verification"]["forward_calculation"]
        # 出口分率与目标吻合
        assert fwd["inputs"]["y2"] == pytest.approx(y2, abs=1e-9)
        assert r["verification"]["recomputed_y2"] == pytest.approx(y2, abs=1e-9)
        # 反解点上的塔高恰好把可用高度用满（容差内）
        assert fwd["Z"] == pytest.approx(z, rel=1e-7)
        # 衡算严格自洽
        assert abs(r["verification"]["mass_balance_residual"]) < 1e-9
        # 四端点与衡算：x1 = x2 + G/L·(y1-y2)
        assert fwd["inputs"]["x1"] == pytest.approx(
            (BASE["y1"] - y2) / r["solution"]["L_over_G"], rel=1e-9
        )

    @pytest.mark.parametrize("z", [0.5, 1.0, 2.0, 3.0])
    def test_outlet_fixed_L_recomputed_height_within_limit(self, settings, z):
        r = inverse.solve_outlet_for_height(
            BASE, z, settings, fixed_q=1.6, optimality_gap_pct=1.0
        )
        fwd = r["verification"]["forward_calculation"]
        assert fwd["Z"] <= z * (1 + 1e-8)
        assert fwd["Z"] == pytest.approx(z, rel=1e-7)

    @pytest.mark.parametrize("z", [0.5, 1.0, 2.0, 3.0])
    def test_outlet_free_practical_height_within_limit(self, settings, z):
        r = inverse.solve_outlet_for_height(
            BASE, z, settings, fixed_q=None, optimality_gap_pct=1.0
        )
        fwd = r["verification"]["forward_calculation"]
        assert fwd["Z"] <= z * (1 + 1e-8)
        assert fwd["Z"] == pytest.approx(z, rel=1e-6)


# ------------------------------------------------------- 单调性：目标越苛刻越贵


class TestMonotonicity:
    def test_harder_target_needs_more_liquid(self, settings):
        """分离目标加深一档 -> 反解 L 单调变大。"""
        qs = []
        for y2 in (0.05, 0.03, 0.02, 0.015, 0.01):
            # 给每个目标足够（但相同量级）的可用塔高，使解都在内部
            r = inverse.solve_flow_for_target(BASE, y2, 4.0, settings)
            qs.append(r["solution"]["L_over_G"])
        assert qs == sorted(qs)
        assert qs[0] < qs[-1]

    def test_harder_target_needs_taller_minimum_height(self, settings):
        """分离目标加深一档 -> 所需理论最小塔高 Z_min 单调变高。"""
        zs = [
            inverse.limiting_outlet(BASE)["Z_min_at"](y2)
            for y2 in (0.05, 0.03, 0.02, 0.01)
        ]
        assert zs == sorted(zs)
        assert zs[0] < zs[-1]

    def test_taller_column_reaches_deeper_outlet(self, settings):
        """塔越给越高 -> 自由流量模式的最优出口分率单调变深（变小）。"""
        ys = []
        for z in (1.0, 1.5, 2.0, 3.0, 4.0):
            r = inverse.solve_outlet_for_height(
                BASE, z, settings, fixed_q=None, optimality_gap_pct=1.0
            )
            ys.append(r["theoretical_best"]["y2"])
        assert ys == sorted(ys, reverse=True)
        assert ys[0] > ys[-1]

    def test_zero_gap_without_fixed_flow_is_rejected(self, settings):
        """自由流量 + gap=0：理论最优只在 L→∞ 取得，须明确拒绝而非求根失败。"""
        with pytest.raises(CalculationError) as ei:
            inverse.solve_outlet_for_height(
                BASE, 2.0, settings, fixed_q=None, optimality_gap_pct=0.0
            )
        assert ei.value.code == "INVALID_SOLVE_REQUEST"

    def test_more_liquid_at_fixed_height_reaches_deeper_outlet(self, settings):
        """同一塔高下，固定的 L 越大 -> 能达到的 y2 越深。"""
        ys = []
        for q in (1.2, 1.6, 2.0, 3.0):
            r = inverse.solve_outlet_for_height(
                BASE, 3.0, settings, fixed_q=q, optimality_gap_pct=1.0
            )
            ys.append(r["theoretical_best"]["y2"])
        assert ys == sorted(ys, reverse=True)
        assert ys[0] > ys[-1]


# ------------------------------------------------------- 可行性边界


class TestInfeasibility:
    def test_target_below_equilibrium_limit(self, settings):
        """y2=0 ≤ m·x2=0：越过理论极限，必须报不可行并给缺口。"""
        exc = expect_infeasible(
            lambda: inverse.solve_flow_for_target(BASE, 0.0, 100.0, settings)
        )
        assert exc.details["kind"] == "target_below_equilibrium_limit"
        assert exc.details["y2_star"] == 0.0
        assert exc.details["shortfall"] > 0

    def test_target_within_pinch_tolerance_band(self, settings):
        """y2 落在 m·x2 的夹点容差带内：与正算夹点判定同口径拒绝。"""
        floor = inverse.pinch_floor(BASE)
        expect_infeasible(
            lambda: inverse.solve_flow_for_target(BASE, floor * 0.5, 100.0, settings)
        )

    def test_target_just_above_floor_is_feasible_but_expensive(self, settings):
        """容差带之上一点点：可行，反解出很大但有限的 L，正算复算仍是非夹点。"""
        floor = inverse.pinch_floor(BASE)
        r = inverse.solve_flow_for_target(BASE, floor * 5.0, 60.0, settings)
        assert math.isfinite(r["solution"]["L"])
        assert r["verification"]["forward_calculation"]["pinch_limited"] is False

    def test_height_below_theoretical_minimum(self, settings):
        """可用高度 < Z_min（ln5≈1.609）：报缺口，不硬吐夹点大数。"""
        exc = expect_infeasible(
            lambda: inverse.solve_flow_for_target(BASE, 0.02, 1.0, settings)
        )
        assert exc.details["kind"] == "height_below_minimum"
        assert exc.details["z_min"] == pytest.approx(math.log(5.0))
        assert exc.details["height_shortfall"] == pytest.approx(math.log(5.0) - 1.0)

    def test_target_above_inlet_rejected(self, settings):
        expect_infeasible(
            lambda: inverse.solve_flow_for_target(BASE, 0.12, 1.0, settings)
        )

    def test_inlet_at_equilibrium_rejected(self, settings):
        """进气 y1 已贴平衡线：没有推动力，任何目标都不可行。"""
        p = dict(BASE, y1=0.05, x2=0.05, m=1.0)  # y1-mx2=0
        expect_infeasible(lambda: inverse.solve_flow_for_target(p, 0.02, 3.0, settings))
        expect_infeasible(
            lambda: inverse.solve_outlet_for_height(
                p, 3.0, settings, fixed_q=None, optimality_gap_pct=1.0
            )
        )

    def test_saturated_solvent_x2_one_rejected(self, settings):
        p = dict(BASE, x2=1.0)
        expect_infeasible(lambda: inverse.solve_flow_for_target(p, 0.05, 3.0, settings))
        expect_infeasible(
            lambda: inverse.solve_outlet_for_height(
                p, 3.0, settings, fixed_q=1.6, optimality_gap_pct=1.0
            )
        )

    def test_fixed_flow_lambda_gt_one_has_NOG_cap(self, settings):
        """q<m（λ>1）：NOG 有上界，塔高超过上界即不可行，并指出需加大 L。"""
        exc = expect_infeasible(
            lambda: inverse.solve_outlet_for_height(
                BASE, 2.0, settings, fixed_q=0.5, optimality_gap_pct=1.0
            )
        )
        assert exc.details["kind"] == "fixed_flow_lambda_gt_one"
        lam = 2.0
        assert exc.details["NOG_cap"] == pytest.approx(math.log(lam) / (lam - 1.0))

    def test_fixed_flow_lambda_equals_one_is_feasible(self, settings):
        """λ=1 平行支无有限 NOG 上界：高塔仍可达，走常数推动力支。"""
        r = inverse.solve_outlet_for_height(
            BASE, 3.0, settings, fixed_q=1.0, optimality_gap_pct=1.0
        )
        fwd = r["verification"]["forward_calculation"]
        assert fwd["integral_method"] == "constant_force"
        assert fwd["Z"] <= 3.0 + 1e-9

    def test_nonpositive_height_rejected(self, settings):
        expect_infeasible(
            lambda: inverse.solve_outlet_for_height(
                BASE, 0.0, settings, fixed_q=1.6, optimality_gap_pct=1.0
            )
        )

    def test_boundary_at_limit_consistent_with_forward_pinch(self, settings):
        """恰好落在极限上的口径必须与正算夹点判定一致：
        正算在 Δ2 ≤ pinch_tolerance 时判夹点，反解在同一边界判不可行。"""
        floor = inverse.pinch_floor(BASE)
        # 正算侧：构造 y2=floor 的自洽操作点应被判夹点（任何 L）
        q = 1.6
        x1 = (BASE["y1"] - floor) / q
        with pytest.raises(CalculationError) as ei:
            engine.run_calculation(dict(BASE, L=q, y2=floor, x1=x1))
        assert ei.value.code == "PINCH_LIMITED"
        # 反解侧：同一目标判 TARGET_INFEASIBLE
        expect_infeasible(
            lambda: inverse.solve_flow_for_target(BASE, floor, 60.0, settings)
        )


# ------------------------------------------------------- 松目标：偏小的解


class TestLooseTarget:
    def test_zero_removal_returns_smallest_flow(self, settings):
        """y2=y1（零脱除）：NOG=Z=0，返回数值下限流量而非加码。"""
        r = inverse.solve_flow_for_target(BASE, 0.10, 0.0, settings)
        assert r["degenerate"] is True
        assert r["solution"]["L_over_G"] == settings["q_floor"]
        assert r["verification"]["forward_calculation"]["Z"] == 0.0

    def test_loose_target_returns_smallest_physical_flow(self, settings):
        """松到物理最小液气比即可满足时：返回该偏小解（x1 贴 1 或 q 贴 q_min）。"""
        r = inverse.solve_flow_for_target(BASE, 0.09, 100.0, settings)
        x1 = r["solution"]["x1"]
        q = r["solution"]["L_over_G"]
        # 物理边界：x1=1（饱和）或 q=q_min（夹点）；此处 x2=0、m=1，
        # q_sat=(y1-y2)/(1-x2)=0.01 远大于 q_min=0.01 同量级
        assert x1 <= 1.0 + 1e-9
        assert r["verification"]["forward_calculation"]["Z"] <= 100.0

    def test_m_zero_flow_independent_of_height(self, settings):
        p = dict(BASE, m=0.0)
        r = inverse.solve_flow_for_target(p, 0.02, 5.0, settings)
        assert r["degenerate"] is True
        # m=0 时 Z 恒为 ln(y1/y2)，L 取物理最小值 q=(y1-y2)/(1-x2)=0.08
        assert r["solution"]["L_over_G"] == pytest.approx(0.08)
        assert r["verification"]["forward_calculation"]["Z"] == pytest.approx(math.log(5.0))


# ------------------------------------------------------- 迭代设置与不收敛


class TestSolverSettings:
    def test_small_iteration_budget_reports_nonconvergence(self, settings):
        s = dict(settings, max_iter=2.0)
        with pytest.raises(CalculationError) as ei:
            inverse.solve_flow_for_target(BASE, 0.02, 2.5, s)
        assert ei.value.code == "SOLVER_DID_NOT_CONVERGE"
        assert ei.value.details["iterations"] == 2

    def test_loose_tolerance_converges_faster(self, settings):
        tight = inverse.solve_flow_for_target(
            BASE, 0.02, 2.5, dict(settings, x_tol_rel=1e-12)
        )
        loose = inverse.solve_flow_for_target(
            BASE, 0.02, 2.5, dict(settings, x_tol_rel=1e-4)
        )
        assert loose["solver"]["iterations"] < tight["solver"]["iterations"]
        # 松容差的解仍在其容差尺度内自洽
        assert loose["verification"]["forward_calculation"]["Z"] == pytest.approx(2.5, rel=1e-4)

    def test_settings_validation_rejects_nonpositive(self):
        with pytest.raises(CalculationError):
            inverse.resolve_settings({"solver_settings": {"max_iterations": 0}})
        with pytest.raises(CalculationError):
            inverse.resolve_settings({"solver_settings": {"L_over_G_floor": -1.0}})

    def test_floor_must_be_below_ceiling(self):
        with pytest.raises(CalculationError) as ei:
            inverse.resolve_settings(
                {"solver_settings": {"L_over_G_floor": 10.0, "L_over_G_ceiling": 1.0}}
            )
        assert ei.value.code == "INVALID_SOLVE_REQUEST"


# ------------------------------------------------------- 传质参数联动


class TestTransferParameterLinkage:
    def test_better_Kya_needs_less_height_same_flow(self, settings):
        """Kya 加倍 -> HOG 减半：同一目标 / 同一可用高度下反解 L 不应变大。"""
        r1 = inverse.solve_flow_for_target(BASE, 0.02, 2.4434419516644135, settings)
        p2 = dict(BASE, Kya=2.0)
        # Kya 加倍后只需一半塔高：给 2.443 的高度对新塔是"富余"，
        # 反解出的 q 应不大于原来的（更少吸收剂即可）
        r2 = inverse.solve_flow_for_target(p2, 0.02, 2.4434419516644135, settings)
        assert r2["solution"]["L_over_G"] <= r1["solution"]["L_over_G"] + 1e-9
        assert r2["verification"]["forward_calculation"]["HOG"] == 0.5

    def test_nonzero_x2_shifts_equilibrium_limit(self, settings):
        """x2=0.02, m=1 -> 理论极限 y2*=0.02；y2=0.01 不可行。"""
        p = dict(BASE, x2=0.02)
        assert inverse.limiting_outlet(p)["y2_star"] == pytest.approx(0.02)
        expect_infeasible(lambda: inverse.solve_flow_for_target(p, 0.01, 10.0, settings))
        # 极限之上可行
        r = inverse.solve_flow_for_target(p, 0.03, 3.0, settings)
        assert r["verification"]["recomputed_y2"] == pytest.approx(0.03, abs=1e-9)
