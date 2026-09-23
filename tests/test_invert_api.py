"""反解 HTTP 接口：单条 / 批量隔离 / 工况档底座 / 错误外壳。"""

from __future__ import annotations

import concurrent.futures
import math

DEMO_BASE = {"G": 1.0, "Kya": 1.0, "a": 1.0, "S": 1.0,
             "y1": 0.10, "x2": 0.0, "m": 1.0}
ZDEMO = 2.4434419516644135


class TestInvertEndpoint:
    def test_fixed_separation_with_height(self, client):
        r = client.post("/invert", json={**DEMO_BASE, "target_y2": 0.02, "Z": ZDEMO})
        assert r.status_code == 200
        d = r.json()
        assert d["mode"] == "fixed_separation"
        assert abs(d["solution"]["L_over_G"] - 1.6) < 1e-7
        assert abs(d["solution"]["x1"] - 0.05) < 1e-7
        # 完整正算复算结果一并给出
        assert abs(d["verification"]["Z"] - ZDEMO) < 1e-7
        assert d["verification"]["NOG"] > 0
        assert d["verification"]["distance_to_pinch"]["pinch_location"] in ("bottom", "top")
        assert d["solver"]["converged"] is True

    def test_profile_used_as_base_only(self, client):
        """点名示范工况档作底座：档内 L/y2/x1 不得被当作固定量。"""
        r = client.post("/invert", json={
            "profile": "air_water_demo", "target_y2": 0.02, "Z": ZDEMO
        })
        assert r.status_code == 200
        d = r.json()
        assert abs(d["solution"]["L_over_G"] - 1.6) < 1e-7
        assert abs(d["solution"]["x1"] - 0.05) < 1e-7

    def test_profile_plus_inline_override(self, client):
        r = client.post("/invert", json={
            "profile": "air_water_demo", "Kya": 2.0, "target_y2": 0.02,
            "design_factor": 2.0,
        })
        assert r.status_code == 200
        d = r.json()
        # Kya 加倍 -> HOG 减半 -> 同分离所需 Z 减半
        assert d["verification"]["HOG"] == 0.5
        assert abs(d["verification"]["Z"] - ZDEMO / 2) < 1e-7

    def test_removal_percent_via_http(self, client):
        r = client.post("/invert", json={**DEMO_BASE, "removal_pct": 90, "design_factor": 1.5})
        assert r.status_code == 200
        d = r.json()
        assert abs(d["target"]["y2"] - 0.01) < 1e-12

    def test_fixed_height_mode(self, client):
        r = client.post("/invert", json={
            **DEMO_BASE, "mode": "fixed_height", "Z_max": ZDEMO, "limit_band": 0.01
        })
        assert r.status_code == 200
        d = r.json()
        assert d["fixed"]["Z_max"] == ZDEMO
        assert abs(d["fixed"]["Z_used"] - ZDEMO) < 1e-7
        # 理论极限 y2_lim = 0.1·exp(-2.44344)
        assert abs(d["fixed"]["y2_best_infinite_solvent_limit"]
                   - 0.1 * math.exp(-ZDEMO)) < 1e-12

    def test_fixed_height_with_explicit_target(self, client):
        r = client.post("/invert", json={
            **DEMO_BASE, "mode": "fixed_height", "Z_max": ZDEMO, "target_y2": 0.02
        })
        assert r.status_code == 200
        assert abs(r.json()["solution"]["L_over_G"] - 1.6) < 1e-7

    def test_unreachable_target_422_with_gap(self, client):
        r = client.post("/invert", json={**DEMO_BASE, "target_y2": 0.0, "Z": 3.0})
        assert r.status_code == 422
        body = r.json()["error"]
        assert body["code"] == "TARGET_UNREACHABLE"
        assert "极限" in body["message"]

    def test_height_short_422(self, client):
        r = client.post("/invert", json={**DEMO_BASE, "target_y2": 0.02, "Z": 1.5})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "TARGET_UNREACHABLE"

    def test_no_control_400(self, client):
        r = client.post("/invert", json={**DEMO_BASE, "target_y2": 0.02})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVERT_SPEC_INVALID"

    def test_forbidden_y2_x1_422(self, client):
        # y2/x1 在结构层即被拒（反解中它们是目标 / 衡算导出量，不属于底座）
        for field in ("y2", "x1"):
            r = client.post("/invert", json={**DEMO_BASE, field: 0.02, "Z": 3.0})
            assert r.status_code == 422

    def test_schema_strictness(self, client):
        # 字符串 / NaN / 未知字段在结构层就被拒
        assert client.post("/invert", json={**DEMO_BASE, "G": "1", "Z": 3}).status_code == 422
        assert client.post("/invert", json={**DEMO_BASE, "Z": 3, "bogus": 1}).status_code == 422
        assert client.post("/invert", json={**DEMO_BASE, "xtol": "1e-8"}).status_code == 422

    def test_solver_did_not_converge_reported(self, client):
        r = client.post("/invert", json={
            **DEMO_BASE, "target_y2": 0.02, "Z": ZDEMO,
            "xtol": 1e-16, "max_iterations": 5,
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "SOLVER_DID_NOT_CONVERGE"

    def test_unknown_profile_404(self, client):
        r = client.post("/invert", json={"profile": "nope", "target_y2": 0.02, "Z": 3.0})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "PROFILE_NOT_FOUND"


class TestInvertBatchEndpoint:
    def test_batch_isolates_failures(self, client):
        body = {"cases": [
            {**DEMO_BASE, "target_y2": 0.02, "design_factor": 2.0},       # 可行
            {**DEMO_BASE, "target_y2": 0.0, "Z": 3.0},                    # 不可行
            {**DEMO_BASE, "mode": "fixed_height", "Z_max": ZDEMO},        # 可行
            {**DEMO_BASE, "target_y2": 0.02},                             # 规格不全
            {**DEMO_BASE, "G": -1.0, "target_y2": 0.02, "Z": 3.0},         # 非法值
            {"profile": "ghost", "target_y2": 0.02, "Z": 3.0},            # 无工况档
            {**DEMO_BASE, "target_y2": 0.02, "L_over_G": 2.0},            # 可行直给
        ]}
        r = client.post("/invert/batch", json=body)
        assert r.status_code == 200
        assert r.json()["count"] == 7
        results = r.json()["results"]
        assert [it["index"] for it in results] == list(range(7))
        assert results[0]["ok"] is True
        assert abs(results[0]["result"]["solution"]["L_over_G"] - 1.6) < 1e-9
        assert results[1]["ok"] is False and results[1]["error"]["code"] == "TARGET_UNREACHABLE"
        assert results[2]["ok"] is True and results[2]["result"]["mode"] == "fixed_height"
        assert results[3]["ok"] is False and results[3]["error"]["code"] == "INVERT_SPEC_INVALID"
        assert results[4]["ok"] is False and results[4]["error"]["code"] == "NON_POSITIVE_VALUE"
        assert results[5]["ok"] is False and results[5]["error"]["code"] == "PROFILE_NOT_FOUND"
        assert results[6]["ok"] is True

    def test_batch_neighbor_independence(self, client):
        """不可行条目夹在中间，前后可行条目的反解互不污染。"""
        body = {"cases": [
            {**DEMO_BASE, "target_y2": 0.03, "design_factor": 2.0},
            {**DEMO_BASE, "target_y2": 0.0, "Z": 3.0},
            {**DEMO_BASE, "target_y2": 0.03, "design_factor": 2.0},
        ]}
        results = client.post("/invert/batch", json=body).json()["results"]
        assert results[0]["result"]["solution"] == results[2]["result"]["solution"]
        assert results[1]["ok"] is False

    def test_batch_empty_rejected(self, client):
        assert client.post("/invert/batch", json={"cases": []}).status_code == 422

    def test_batch_with_profiles(self, client):
        client.put("/profiles/towerX", json={
            "name": "towerX",
            "params": {**DEMO_BASE, "L": 9.9, "y2": 0.02, "x1": 0.05, "Kya": 0.5},
        })
        # 档内的 L/y2/x1 作底座时必须被剔除，仅 Kya=0.5 生效
        r = client.post("/invert/batch", json={"cases": [
            {"profile": "towerX", "target_y2": 0.02, "design_factor": 2.0},
        ]})
        result = r.json()["results"][0]
        assert result["ok"] is True, result
        assert result["result"]["verification"]["HOG"] == 2.0  # G/(Kya·a·S)=1/0.5
        assert abs(result["result"]["solution"]["L_over_G"] - 1.6) < 1e-9


class TestInvertConcurrency:
    def test_concurrent_inversions_are_independent(self, client):
        """不同目标并发反解，各自求根状态互不串（求解器不得持有共享可变状态）。"""
        targets = [0.05, 0.03, 0.02, 0.015, 0.01] * 6

        def one(y2):
            r = client.post("/invert", json={**DEMO_BASE, "target_y2": y2, "Z": 3.0})
            d = r.json()
            return y2, d["solution"]["L_over_G"], d["verification"]["inputs"]["y2"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for y2, lg, y2_back in pool.map(one, targets):
                assert abs(y2_back - y2) < 1e-12
                assert lg > 0

    def test_concurrent_invert_and_calculate_mixed(self, client):
        """反解与正算并发，彼此不污染。"""
        def work(i):
            r1 = client.post("/invert", json={
                **DEMO_BASE, "target_y2": 0.02, "Z": 2.0 + 0.01 * i
            })
            r2 = client.post("/calculate", json={
                **DEMO_BASE, "L": 1.6, "y2": 0.02, "x1": 0.05
            })
            return r1.json()["verification"]["Z"], r2.json()["Z"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for z_inv, z_fwd in pool.map(work, range(24)):
                assert abs(z_inv - 2.4434419516644135) > 0  # 各自 Z 不同
                assert abs(z_fwd - 2.4434419516644135) < 1e-12
