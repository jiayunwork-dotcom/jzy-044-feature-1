"""反解 HTTP 接口：单条 / 批量、profile 叠加、错误外壳、逐条隔离。"""

from __future__ import annotations

import math

import pytest

BASE = {
    "G": 1.0, "Kya": 1.0, "a": 1.0, "S": 1.0,
    "y1": 0.10, "x2": 0.0, "m": 1.0,
}
Z_DEMO = 2.4434419516644135


class TestSolveFlowForTarget:
    def test_inline_target_y2(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.02, "z_available": Z_DEMO,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["mode"] == "flow_for_target"
        assert d["solution"]["L"] == pytest.approx(1.6, abs=1e-8)
        assert d["solution"]["x1"] == pytest.approx(0.05, abs=1e-9)
        assert d["solver"]["converged"] is True
        # 内嵌正算复算，自洽
        fwd = d["verification"]["forward_calculation"]
        assert fwd["Z"] == pytest.approx(Z_DEMO, rel=1e-9)
        assert fwd["NOG"] == pytest.approx(2.4434419516644135, rel=1e-9)
        assert fwd["inputs"]["y2"] == pytest.approx(0.02, abs=1e-10)
        assert d["verification"]["recomputed_y2"] == pytest.approx(0.02, abs=1e-10)
        # 可行性边界信息齐全
        assert d["feasibility_limits"]["y2_star"] == 0.0
        assert d["feasibility_limits"]["minimum_L_over_G"] == pytest.approx(0.8)

    def test_removal_pct_equivalent_to_y2(self, client):
        """80% 脱除等价于 y2 = y1·(1-0.8) = 0.02。"""
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "removal_pct": 80.0, "z_available": Z_DEMO,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["solution"]["L"] == pytest.approx(1.6, abs=1e-8)
        assert d["target"]["specified_by"] == "removal_pct"
        assert d["target"]["y2"] == pytest.approx(0.02, abs=1e-12)
        assert d["target"]["removal_pct"] == 80.0

    def test_profile_base_with_inline_target(self, client):
        """点名示范工况档提供底座，只叠加分离目标与塔高。"""
        r = client.post("/solve", json={
            "mode": "flow_for_target", "profile": "air_water_demo",
            "target_y2": 0.02, "z_available": Z_DEMO,
        })
        assert r.status_code == 200
        assert r.json()["solution"]["L"] == 1.6

    def test_profile_base_override_one_field(self, client):
        """工况档打底 + 内联覆盖 Kya：HOG 随之改变。"""
        r = client.post("/solve", json={
            "mode": "flow_for_target", "profile": "air_water_demo",
            "target_y2": 0.02, "z_available": Z_DEMO / 2, "Kya": 2.0,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["verification"]["forward_calculation"]["HOG"] == 0.5

    def test_infeasible_below_equilibrium(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.0, "z_available": 10.0,
        })
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "TARGET_INFEASIBLE"
        assert body["details"]["kind"] == "target_below_equilibrium_limit"
        assert body["details"]["shortfall"] > 0

    def test_infeasible_height_too_short(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.02, "z_available": 1.0,
        })
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "TARGET_INFEASIBLE"
        assert body["details"]["kind"] == "height_below_minimum"
        assert body["details"]["height_shortfall"] > 0

    def test_removal_100pct_infeasible(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "removal_pct": 100.0, "z_available": 10.0,
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "TARGET_INFEASIBLE"

    def test_missing_target_400(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target", "z_available": 3.0,
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SOLVE_REQUEST"

    def test_both_targets_rejected(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.02, "removal_pct": 80.0, "z_available": 3.0,
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SOLVE_REQUEST"

    def test_missing_height_400(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target", "target_y2": 0.02,
        })
        assert r.status_code == 400

    def test_missing_base_field_400(self, client):
        r = client.post("/solve", json={
            "mode": "flow_for_target", "target_y2": 0.02, "z_available": 3.0,
            "G": 1.0,
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "MISSING_FIELD"

    def test_L_y2_x1_not_accepted_in_solve_base(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.02, "z_available": 3.0, "L": 999.0,
        })
        assert r.status_code == 422  # extra="forbid"

    def test_solver_no_convergence_reported(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": 0.02, "z_available": 2.5,
            "solver_settings": {"max_iterations": 2},
        })
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "SOLVER_DID_NOT_CONVERGE"
        assert body["details"]["iterations"] == 2

    def test_loose_target_zero_removal(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "removal_pct": 0.0, "z_available": 0.0,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["degenerate"] is True
        assert d["verification"]["forward_calculation"]["Z"] == 0.0

    def test_string_and_bool_rejected(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": "0.02", "z_available": 3.0,
        })
        assert r.status_code == 422
        r = client.post("/solve", json={
            **BASE, "mode": "flow_for_target",
            "target_y2": True, "z_available": 3.0,
        })
        assert r.status_code == 422


class TestSolveOutletForHeight:
    def test_free_flow_returns_best_and_practical(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height", "z_max": Z_DEMO,
        })
        assert r.status_code == 200
        d = r.json()
        # 理论最优 y2_best = 0.1·e^-N
        assert d["theoretical_best"]["y2"] == pytest.approx(0.10 * math.exp(-Z_DEMO), rel=1e-9)
        assert d["theoretical_best"]["L_over_G"] == "Infinity"  # JSON inf
        # 实操点（默认 1% 回退）
        prac = d["practical"]
        assert prac["y2"] == d["theoretical_best"]["y2"] * 1.01
        assert prac["z_required"] <= Z_DEMO + 1e-9
        fwd = d["verification"]["forward_calculation"]
        assert fwd["Z"] == prac["z_required"]

    def test_custom_gap(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height", "z_max": Z_DEMO,
            "optimality_gap_pct": 5.0,
        })
        d = r.json()
        assert d["practical"]["y2"] == d["theoretical_best"]["y2"] * 1.05

    def test_zero_gap_rejected(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height", "z_max": Z_DEMO,
            "optimality_gap_pct": 0.0,
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SOLVE_REQUEST"

    def test_fixed_L(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height",
            "z_max": Z_DEMO, "fixed_L": 1.6,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["fixed_L_over_G"] == 1.6
        assert d["theoretical_best"]["y2"] == pytest.approx(0.02, abs=1e-8)
        assert d["practical"] is None

    def test_fixed_L_over_G(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height",
            "z_max": Z_DEMO, "fixed_L_over_G": 1.6,
        })
        assert r.json()["theoretical_best"]["y2"] == pytest.approx(0.02, abs=1e-8)

    def test_fixed_flow_lambda_gt_one_infeasible(self, client):
        r = client.post("/solve", json={
            **BASE, "mode": "outlet_for_height",
            "z_max": 2.0, "fixed_L_over_G": 0.5,
        })
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "TARGET_INFEASIBLE"
        assert body["details"]["kind"] == "fixed_flow_lambda_gt_one"

    def test_missing_z_max_400(self, client):
        r = client.post("/solve", json={**BASE, "mode": "outlet_for_height"})
        assert r.status_code == 400

    def test_bad_mode_400(self, client):
        r = client.post("/solve", json={**BASE, "mode": "nope", "z_max": 1.0})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SOLVE_REQUEST"


class TestSolveBatch:
    def test_batch_mixed_isolation(self, client):
        body = {"cases": [
            {**BASE, "mode": "flow_for_target", "target_y2": 0.02, "z_available": Z_DEMO},
            {**BASE, "mode": "flow_for_target", "target_y2": 0.0, "z_available": 5.0},  # 不可行
            {"mode": "flow_for_target", "profile": "air_water_demo",
             "removal_pct": 80.0, "z_available": Z_DEMO},                              # profile
            {**BASE, "mode": "outlet_for_height", "z_max": Z_DEMO, "fixed_L": 1.6},    # 定塔高
            {**BASE, "mode": "flow_for_target", "target_y2": 0.02},                    # 缺塔高
            {**BASE, "mode": "flow_for_target", "target_y2": 0.02, "z_available": 1.0},  # 不可行
        ]}
        r = client.post("/solve/batch", json=body)
        assert r.status_code == 200
        assert r.json()["count"] == 6
        results = r.json()["results"]
        assert [it["index"] for it in results] == list(range(6))
        assert results[0]["ok"] is True
        assert results[0]["result"]["solution"]["L"] == 1.6
        assert results[1]["ok"] is False
        assert results[1]["error"]["code"] == "TARGET_INFEASIBLE"
        assert results[1]["error"]["details"]["kind"] == "target_below_equilibrium_limit"
        assert results[2]["ok"] is True
        assert results[2]["result"]["solution"]["L"] == 1.6
        assert results[3]["ok"] is True
        assert results[3]["result"]["theoretical_best"]["y2"] == pytest.approx(0.02, abs=1e-8)
        assert results[4]["ok"] is False
        assert results[4]["error"]["code"] == "INVALID_SOLVE_REQUEST"
        assert results[5]["ok"] is False
        assert results[5]["error"]["details"]["kind"] == "height_below_minimum"

    def test_batch_failure_does_not_poison_neighbors(self, client):
        body = {"cases": [
            {**BASE, "mode": "flow_for_target", "target_y2": 0.02, "z_available": Z_DEMO},
            {**BASE, "mode": "weird"},
            {**BASE, "mode": "flow_for_target", "target_y2": 0.03, "z_available": 2.0},
        ]}
        results = client.post("/solve/batch", json=body).json()["results"]
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False and results[1]["error"]["code"] == "INVALID_SOLVE_REQUEST"
        assert results[2]["ok"] is True

    def test_batch_empty_rejected(self, client):
        assert client.post("/solve/batch", json={"cases": []}).status_code == 422

    def test_batch_endpoint_listed_at_root(self, client):
        endpoints = client.get("/").json()["endpoints"]
        assert "POST /solve" in endpoints
        assert "POST /solve/batch" in endpoints
