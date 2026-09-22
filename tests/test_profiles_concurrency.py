"""工况档持久化与并发隔离。"""

from __future__ import annotations

import concurrent.futures
import json
import threading

from app.main import create_app
from app.profiles import BUILTIN_PROFILE, ProfileStore
from fastapi.testclient import TestClient

DEMO_PARAMS = {
    "G": 1.0, "L": 1.6, "Kya": 1.0, "a": 1.0, "S": 1.0,
    "y1": 0.10, "y2": 0.02, "x1": 0.05, "x2": 0.0, "m": 1.0,
}


class TestProfileCRUD:
    def test_builtin_exists_and_matches_handcalc(self, client):
        r = client.get(f"/profiles/{BUILTIN_PROFILE}")
        assert r.status_code == 200
        rec = r.json()
        assert rec["builtin"] is True
        assert rec["params"] == DEMO_PARAMS

    def test_list_includes_builtin(self, client):
        names = [p["name"] for p in client.get("/profiles").json()["profiles"]]
        assert BUILTIN_PROFILE in names

    def test_register_and_use_named_profile(self, client):
        r = client.put("/profiles/tower_A", json={
            "name": "tower_A",
            "description": "拉西环 25mm 工况",
            "params": dict(DEMO_PARAMS, Kya=0.8),
        })
        assert r.status_code == 200
        assert r.json()["name"] == "tower_A"

        used = client.post("/calculate", json={"profile": "tower_A"}).json()
        inline = client.post("/calculate", json=dict(DEMO_PARAMS, Kya=0.8)).json()
        assert used["HOG"] == inline["HOG"] == 1.25

    def test_profile_update_merges_fields(self, client):
        client.put("/profiles/tower_B", json={"name": "tower_B", "params": dict(DEMO_PARAMS)})
        r = client.put("/profiles/tower_B", json={"name": "tower_B", "params": {"Kya": 2.5}})
        # 部分更新：保留旧参数，覆盖给入字段
        assert r.status_code == 200
        assert r.json()["params"]["Kya"] == 2.5
        assert r.json()["params"]["y1"] == 0.10

    def test_first_registration_requires_all_fields(self, client):
        r = client.put("/profiles/incomplete", json={"name": "incomplete", "params": {"G": 1.0}})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "PROFILE_INVALID"

    def test_unknown_field_in_profile_params_rejected(self, client):
        r = client.put("/profiles/bad", json={"name": "bad", "params": dict(DEMO_PARAMS, nope=1)})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "PROFILE_INVALID"

    def test_delete_custom_profile(self, client):
        client.put("/profiles/tmp_case", json={"name": "tmp_case", "params": DEMO_PARAMS})
        assert client.delete("/profiles/tmp_case").status_code == 204
        assert client.get("/profiles/tmp_case").status_code == 404

    def test_builtin_is_readonly(self, client):
        r1 = client.put(f"/profiles/{BUILTIN_PROFILE}",
                        json={"name": BUILTIN_PROFILE, "params": DEMO_PARAMS})
        assert r1.status_code == 409
        assert r1.json()["error"]["code"] == "PROFILE_READONLY"
        r2 = client.delete(f"/profiles/{BUILTIN_PROFILE}")
        assert r2.status_code == 409

    def test_delete_missing_404(self, client):
        assert client.delete("/profiles/ghost").status_code == 404

    def test_invalid_profile_name(self, client):
        # 空格不合法；%20 不会被路径归一化吞掉
        r = client.put("/profiles/bad%20name", json={"name": "bad name", "params": DEMO_PARAMS})
        assert r.status_code in (400, 422)
        assert r.json()["error"]["code"] == "PROFILE_NAME_INVALID"

    def test_path_name_mismatch(self, client):
        r = client.put("/profiles/aaa", json={"name": "bbb", "params": DEMO_PARAMS})
        assert r.status_code == 400


class TestPersistence:
    def test_profile_survives_store_reinstantiation(self, tmp_path):
        data_dir = tmp_path / "data"
        app1 = create_app(data_dir)
        c1 = TestClient(app1)
        c1.put("/profiles/persist_me", json={"name": "persist_me", "params": DEMO_PARAMS})

        # 磁盘上确有 JSON，且内容可解析
        f = data_dir / "profiles.json"
        assert f.exists()
        json.loads(f.read_text(encoding="utf-8"))

        # 模拟进程重启：全新 app / 全新 store 实例指向同一目录
        c2 = TestClient(create_app(data_dir))
        r = c2.post("/calculate", json={"profile": "persist_me"})
        assert r.status_code == 200
        assert r.json()["NOG"] == 2.4434419516644135

    def test_builtin_seeded_even_without_file(self, tmp_path):
        store = ProfileStore(tmp_path / "x" / "profiles.json")
        assert BUILTIN_PROFILE in [p["name"] for p in store.list_profiles()]

    def test_corrupt_file_self_heals_to_builtin(self, tmp_path):
        f = tmp_path / "profiles.json"
        f.write_text("{ not json", encoding="utf-8")
        store = ProfileStore(f)
        names = [p["name"] for p in store.list_profiles()]
        assert BUILTIN_PROFILE in names
        # 损坏文件被挪走留证
        assert any(tmp_path.glob("*.corrupt.*"))


class TestConcurrency:
    def test_concurrent_calculations_are_independent(self, client):
        """并发核算各自参数互不串。"""
        kya_values = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0] * 4

        def one(kya):
            r = client.post("/calculate", json=dict(DEMO_PARAMS, Kya=kya))
            d = r.json()
            return kya, d["HOG"], d["NOG"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for kya, hog, nog in pool.map(one, kya_values):
                assert hog == 1.0 / kya
                assert nog == 2.4434419516644135  # NOG 与 Kya 无关，不被写串

    def test_concurrent_profile_registrations_do_not_lose_writes(self, client):
        errors = []

        def register(i):
            try:
                r = client.put(f"/profiles/case_{i}",
                               json={"name": f"case_{i}", "params": dict(DEMO_PARAMS, Kya=1.0 + i)})
                if r.status_code != 200:
                    errors.append((i, r.status_code, r.text))
            except Exception as exc:  # pragma: no cover
                errors.append((i, "exc", str(exc)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(register, range(40)))

        assert errors == []
        profiles = client.get("/profiles").json()["profiles"]
        names = {p["name"] for p in profiles}
        assert {f"case_{i}" for i in range(40)} <= names
        # 每个工况档参数都是自己的
        for i in range(40):
            rec = client.get(f"/profiles/case_{i}").json()
            assert rec["params"]["Kya"] == 1.0 + i

    def test_concurrent_calc_and_registration_mixed(self, client):
        """登记工况与核算同时进行，彼此不污染。"""
        stop = threading.Event()

        def hammer_calc():
            while not stop.is_set():
                d = client.post("/calculate", json=DEMO_PARAMS).json()
                assert d["NOG"] == 2.4434419516644135

        t = threading.Thread(target=hammer_calc)
        t.start()
        for i in range(20):
            client.put(f"/profiles/mix_{i}", json={"name": f"mix_{i}", "params": DEMO_PARAMS})
            client.post("/calculate", json={"profile": f"mix_{i}"})
        stop.set()
        t.join(timeout=10)
