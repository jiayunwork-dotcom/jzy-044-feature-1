"""共享 pytest fixtures：每个测试使用独立的临时工况档目录，互不污染。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def demo_params() -> dict:
    """内置示范工况对应的内联参数（可手算核对）。"""
    return {
        "G": 1.0,
        "L": 1.6,
        "Kya": 1.0,
        "a": 1.0,
        "S": 1.0,
        "y1": 0.10,
        "y2": 0.02,
        "x1": 0.05,
        "x2": 0.0,
        "m": 1.0,
    }


def calc(client, **overrides):
    """便捷封装：直接以 JSON 调用 /calculate。"""
    return client.post("/calculate", json=overrides)
