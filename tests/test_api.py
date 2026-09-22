"""HTTP 接口：单条 / 批量核算、错误外壳。"""

from __future__ import annotations

DEMO = {
    "G": 1.0, "L": 1.6, "Kya": 1.0, "a": 1.0, "S": 1.0,
    "y1": 0.10, "y2": 0.02, "x1": 0.05, "x2": 0.0, "m": 1.0,
}


class TestCalculateEndpoint:
    def test_inline_full_params(self, client):
        r = client.post("/calculate", json=DEMO)
        assert r.status_code == 200
        d = r.json()
        assert d["NOG"] == 2.4434419516644135
        assert d["HOG"] == 1.0
        assert d["Z"] == 2.4434419516644135
        assert d["pinch_limited"] is False
        assert d["distance_to_pinch"]["min_driving_force"] == 0.02

    def test_profile_demo_via_http(self, client):
        r = client.post("/calculate", json={"profile": "air_water_demo"})
        assert r.status_code == 200
        assert r.json()["NOG"] == 2.4434419516644135

    def test_profile_with_override(self, client):
        r = client.post("/calculate", json={"profile": "air_water_demo", "Kya": 2.0})
        d = r.json()
        assert d["HOG"] == 0.5
        assert d["Z"] == 1.2217209758322068

    def test_unknown_profile_404(self, client):
        r = client.post("/calculate", json={"profile": "nope"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "PROFILE_NOT_FOUND"

    def test_pinch_returns_error_not_finite_height(self, client):
        r = client.post("/calculate", json=dict(DEMO, L=0.8, x1=0.10))
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "PINCH_LIMITED"
        assert "液气比" in body["error"]["message"]

    def test_mass_balance_error(self, client):
        r = client.post("/calculate", json=dict(DEMO, x1=0.07))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "MASS_BALANCE_MISMATCH"

    def test_non_positive_error(self, client):
        r = client.post("/calculate", json=dict(DEMO, S=-1))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "NON_POSITIVE_VALUE"

    def test_fraction_range_error(self, client):
        r = client.post("/calculate", json=dict(DEMO, y1=1.5))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "FRACTION_OUT_OF_RANGE"

    def test_degenerate_equal_y(self, client):
        r = client.post("/calculate", json=dict(DEMO, y1=0.05, y2=0.05, x1=0.03, x2=0.03))
        assert r.status_code == 200
        d = r.json()
        assert d["NOG"] == 0.0 and d["Z"] == 0.0 and d["degenerate"] is True

    def test_missing_fields_400(self, client):
        r = client.post("/calculate", json={"G": 1.0})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "MISSING_FIELD"

    def test_string_number_rejected_at_schema(self, client):
        r = client.post("/calculate", json=dict(DEMO, G="1.0"))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

    def test_nan_rejected_at_schema(self, client):
        r = client.post("/calculate", json=dict(DEMO, G="NaN"))
        assert r.status_code == 422

    def test_unknown_field_rejected(self, client):
        r = client.post("/calculate", json=dict(DEMO, bogus=1))
        assert r.status_code == 422


class TestBatchEndpoint:
    def test_batch_mixed_success_and_failures(self, client):
        body = {"cases": [
            DEMO,                                                  # 正常
            dict(DEMO, Kya=2.0),                                   # 正常（联动）
            dict(DEMO, L=0.8, x1=0.10),                            # 夹点
            dict(DEMO, x1=0.07),                                   # 衡算矛盾
            dict(DEMO, G=-1),                                      # 非法
            dict(DEMO, y1=0.05, y2=0.05, x1=0.03, x2=0.03),        # 退化
        ]}
        r = client.post("/calculate/batch", json=body)
        assert r.status_code == 200
        assert r.json()["count"] == 6
        results = r.json()["results"]
        assert [it["index"] for it in results] == list(range(6))
        assert results[0]["ok"] is True and results[0]["result"]["NOG"] == 2.4434419516644135
        assert results[1]["ok"] is True and results[1]["result"]["Z"] == 1.2217209758322068
        assert results[2]["ok"] is False and results[2]["error"]["code"] == "PINCH_LIMITED"
        assert results[3]["ok"] is False and results[3]["error"]["code"] == "MASS_BALANCE_MISMATCH"
        assert results[4]["ok"] is False and results[4]["error"]["code"] == "NON_POSITIVE_VALUE"
        assert results[5]["ok"] is True and results[5]["result"]["Z"] == 0.0

    def test_batch_failure_does_not_poison_neighbors(self, client):
        """坏条目夹在中间，前后好条目结果必须完全独立、不被污染。"""
        body = {"cases": [dict(DEMO, Kya=3.0), dict(DEMO, G=0), dict(DEMO, Kya=5.0)]}
        r = client.post("/calculate/batch", json=body)
        results = r.json()["results"]
        assert results[0]["result"]["HOG"] == 1 / 3
        assert results[2]["result"]["HOG"] == 1 / 5
        assert results[1]["error"]["code"] == "NON_POSITIVE_VALUE"

    def test_batch_empty_rejected(self, client):
        r = client.post("/calculate/batch", json={"cases": []})
        assert r.status_code == 422

    def test_batch_with_profiles(self, client):
        client.put("/profiles/mycase", json={"name": "mycase", "params": dict(DEMO, Kya=4.0)})
        r = client.post("/calculate/batch", json={"cases": [
            {"profile": "air_water_demo"},
            {"profile": "mycase"},
        ]})
        results = r.json()["results"]
        assert results[0]["result"]["HOG"] == 1.0
        assert results[1]["result"]["HOG"] == 0.25


class TestMeta:
    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok", "version": client.get("/").json()["version"]}

    def test_root_lists_endpoints(self, client):
        d = client.get("/").json()
        assert "POST /calculate" in d["endpoints"]
        assert d["builtin_profile"] == "air_water_demo"
