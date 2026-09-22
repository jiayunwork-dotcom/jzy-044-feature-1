"""传质内核：物理联动性质与封闭式取值。

这些用例直接打纯函数引擎，不依赖 HTTP，便于精算核对。
"""

from __future__ import annotations

import math

import pytest

from app import balance, engine, nog


class TestDemoHandCalc:
    """稀相空气吸收示范工况：手算值锁定。"""

    def test_demo_matches_hand_calculation(self, demo_params):
        r = engine.run_calculation(demo_params)
        # Δ1=0.10-0.05=0.05, Δ2=0.02-0=0.02
        assert r["driving_force_bottom"] == pytest.approx(0.05)
        assert r["driving_force_top"] == pytest.approx(0.02)
        # Δlm=(0.05-0.02)/ln(2.5)=0.032740700..., NOG=0.08/Δlm=2.44344...
        delta_lm = (0.05 - 0.02) / math.log(0.05 / 0.02)
        assert delta_lm == pytest.approx(0.03274070004, rel=1e-9)
        assert r["NOG"] == pytest.approx(0.08 / delta_lm, rel=1e-12)
        assert r["NOG"] == pytest.approx(2.4434419517, rel=1e-9)
        assert r["HOG"] == pytest.approx(1.0)  # G/(Kya·a·S)=1/1
        assert r["Z"] == pytest.approx(r["HOG"] * r["NOG"])
        assert r["integral_method"] == "log_mean"
        assert r["slope_ratio"] == pytest.approx(0.625)  # mG/L=1/1.6

    def test_demo_minimum_liquid_to_gas(self, demo_params):
        # (L/G)_min = (y1-y2)/(y1/m-x2) = 0.08/0.10 = 0.8
        assert balance.minimum_liquid_to_gas(demo_params) == pytest.approx(0.8)
        r = engine.run_calculation(demo_params)
        assert r["L_over_G"] == pytest.approx(1.6)
        assert r["L_over_G_excess_pct"] == pytest.approx(100.0)  # 实际高出一倍
        assert r["pinch_limited"] is False

    def test_log_mean_and_absorption_factor_formula_agree(self, demo_params):
        """两种封闭式写法（对数平均推动力 / 吸收因子式）必须一致。"""
        r = engine.run_calculation(demo_params)
        lam = r["slope_ratio"]
        y1, y2, m, x2 = 0.10, 0.02, 1.0, 0.0
        # NOG = 1/(1-λ) · ln[(1-λ)(y1-mx2)/(y2-mx2)+λ]
        nog_alt = math.log((1 - lam) * (y1 - m * x2) / (y2 - m * x2) + lam) / (1 - lam)
        assert r["NOG"] == pytest.approx(nog_alt, rel=1e-12)

    def test_log_mean_symmetric_in_end_order(self):
        """Δlm 对两端顺序不敏感。"""
        assert nog.log_mean_driving_force(0.05, 0.02) == pytest.approx(
            nog.log_mean_driving_force(0.02, 0.05)
        )
        # 两端相等时退化为算术平均
        assert nog.log_mean_driving_force(0.03, 0.03) == pytest.approx(0.03)


class TestPhysicsLinkage:
    """需求点名的三条联动性质。"""

    def test_doubling_Kya_halves_HOG_and_Z_but_NOG_unchanged(self, demo_params):
        base = engine.run_calculation(demo_params)
        doubled = dict(demo_params, Kya=2.0 * demo_params["Kya"])
        r2 = engine.run_calculation(doubled)
        assert r2["NOG"] == pytest.approx(base["NOG"], rel=1e-12)
        assert r2["HOG"] == pytest.approx(base["HOG"] / 2)
        assert r2["Z"] == pytest.approx(base["Z"] / 2)

    def test_increasing_L_over_G_decreases_NOG(self, demo_params):
        """远离夹点区间内加大 L/G，推动力拉开，NOG 单调下降。"""
        nogs = []
        for L in (1.6, 2.0, 2.5, 4.0):
            # 固定其他端点，由衡算重算 x1，保证自洽
            p = dict(demo_params, L=L, x1=demo_params["x2"] + demo_params["G"] / L * 0.08)
            r = engine.run_calculation(p)
            nogs.append(r["NOG"])
        assert nogs == sorted(nogs, reverse=True)
        assert nogs[0] > nogs[-1]
        assert nogs[-1] == pytest.approx(1.8484, abs=1e-3)  # L/G=4（λ=0.25）
        # L/G→∞ 时 λ→0，NOG→ln(y1/y2)=ln 5≈1.609
        p_inf = dict(demo_params, L=1.0e4,
                     x1=demo_params["G"] / 1.0e4 * 0.08)
        assert engine.run_calculation(p_inf)["NOG"] == pytest.approx(math.log(5.0), abs=1e-3)

    def test_equal_inlet_outlet_gas_gives_zero_height(self, demo_params):
        p = dict(demo_params, y1=0.05, y2=0.05, x1=0.03, x2=0.03)
        r = engine.run_calculation(p)
        assert r["degenerate"] is True
        assert r["NOG"] == 0.0
        assert r["Z"] == 0.0
        assert r["pinch_limited"] is False

    def test_HOG_depends_on_G_ka_a_S(self, demo_params):
        base = engine.run_calculation(demo_params)
        # a 翻倍 -> HOG 减半
        r_a2 = engine.run_calculation(dict(demo_params, a=2.0))
        assert r_a2["HOG"] == pytest.approx(base["HOG"] / 2)
        # G 翻倍时必须同时把 L 翻倍（保持 L/G 与端点推动力不变），否则
        # L/G 会跌到最小值而真的夹点——那是另一条物理规律，由夹点测试覆盖
        r_g2 = engine.run_calculation(dict(demo_params, G=2.0, L=3.2))
        assert r_g2["HOG"] == pytest.approx(base["HOG"] * 2)
        assert r_g2["NOG"] == pytest.approx(base["NOG"], rel=1e-12)
        r_s2 = engine.run_calculation(dict(demo_params, S=2.0))
        assert r_s2["HOG"] == pytest.approx(base["HOG"] / 2)


class TestLambdaOneBranch:
    """λ=mG/L=1 时必须走常数推动力退化支，不能套对数平均。"""

    @pytest.fixture
    def flat(self):
        # m=2, G=1, L=2 -> λ=1；衡算 1(0.10-0.04)=2(0.04-0.01)=0.06
        return dict(G=1.0, L=2.0, Kya=0.5, a=1.0, S=1.0,
                    y1=0.10, y2=0.04, x1=0.04, x2=0.01, m=2.0)

    def test_constant_force_branch(self, flat):
        r = engine.run_calculation(flat)
        assert r["slope_ratio"] == pytest.approx(1.0)
        assert r["integral_method"] == "constant_force"
        # 推动力处处相等：Δ1=0.10-2·0.04=0.02, Δ2=0.04-2·0.01=0.02
        assert r["driving_force_bottom"] == pytest.approx(0.02)
        assert r["driving_force_top"] == pytest.approx(0.02)
        assert r["NOG"] == pytest.approx(0.06 / 0.02)  # 3.0
        assert r["HOG"] == pytest.approx(2.0)           # 1/(0.5·1·1)
        assert r["Z"] == pytest.approx(6.0)

    def test_near_unit_lambda_still_uses_log_mean_without_blowup(self, flat):
        """λ 略偏离 1 时回到对数平均支，且两支在 λ→1 处连续。"""
        p = dict(flat, L=2.0 + 1e-6, x1=0.01 + (0.06 / (2.0 + 1e-6)))
        r = engine.run_calculation(p)
        assert r["integral_method"] == "log_mean"
        assert r["NOG"] == pytest.approx(3.0, abs=1e-5)
