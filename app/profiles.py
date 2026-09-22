"""具名工况档的持久化。

* 存储后端：``$SERVICE_DATA_DIR/profiles.json``，原子写（同目录临时文件 +
  ``os.replace``），进程重启 / 容器重建（挂载卷）后仍可点名取用；
* 并发：进程内一把可重入锁串行化所有读改写；写盘前先从磁盘重新加载，
  因此多个 worker / 进程共用同一文件时也不会互相覆盖；
* 内置示范工况 ``air_water_demo`` 随服务提供，只读，不可改 / 删。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .errors import (
    ERR_PROFILE_INVALID,
    ERR_PROFILE_NAME,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROFILE_READONLY,
    CalculationError,
)
from .validation import ALL_FIELDS, FIELD_LABELS, coerce_known_fields

BUILTIN_PROFILE = "air_water_demo"

# 可手算核对的稀相空气-水吸收示范工况：
#   y1=0.10, y2=0.02（80% 脱除），x2=0（纯溶剂），m=1
#   由衡算取 L/G=1.6 -> x1 = x2 + G/L·(y1-y2) = 0.05
#   Δ1=0.05, Δ2=0.02 -> NOG=(0.10-0.02)/Δlm
#   Δlm=(0.05-0.02)/ln(0.05/0.02)=0.032736... -> NOG≈2.4434
#   取 Kya·a·S = G = 1.0 -> HOG=1 m -> Z≈2.4434 m
#   (L/G)_min = (0.10-0.02)/(0.10/1-0)=0.8，实际 L/G 高出 100%
_DEFINITION: dict[str, Any] = {
    "name": BUILTIN_PROFILE,
    "description": (
        "稀相空气-水逆流吸收示范（可手算核对）：y1=0.10, y2=0.02, x2=0, "
        "x1=0.05, m=1, G=L=1 kmol/(m²·h) 量级, Kya=1 kmol/(m³·h), "
        "a=1 m²/m³, S=1 m²；NOG≈2.4434，HOG=1 m，Z≈2.4434 m。"
    ),
    "params": {
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
    },
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_一-鿿][A-Za-z0-9_.\-一-鿿]{0,63}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _builtin_record() -> dict[str, Any]:
    return {
        **_DEFINITION,
        "builtin": True,
        "created_at": "1970-01-01T00:00:00Z",
        "updated_at": "1970-01-01T00:00:00Z",
    }


class ProfileStore:
    """工况档仓库。单进程内单例使用；多进程共用同一 JSON 文件安全。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.PROFILES_FILE
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 内部
    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _raw_load_locked(self) -> dict[str, Any]:
        """从磁盘读取（不加锁，调用方持锁）。文件缺失 / 损坏时自愈。"""
        builtin = {BUILTIN_PROFILE: _builtin_record()}
        if not self.path.exists():
            return builtin
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "profiles" not in data:
                raise ValueError("profiles.json 结构不正确")
            records = data["profiles"]
            if not isinstance(records, dict):
                raise ValueError("profiles.json 中 profiles 不是对象")
        except (json.JSONDecodeError, ValueError, OSError):
            # 损坏文件挪走留证，然后以仅含内置工况的状态重生
            backup = self.path.with_suffix(f".corrupt.{os.getpid()}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return builtin
        # 内置工况永远存在且只读参数以代码定义为准（防手工篡改文件）
        records[BUILTIN_PROFILE] = _builtin_record()
        return records

    def _atomic_save_locked(self, records: dict[str, Any]) -> None:
        self._ensure_dir()
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        payload = {"version": 1, "profiles": records}
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 对外
    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            records = self._raw_load_locked()
        return [self._summarize(rec) for rec in sorted(records.values(), key=lambda r: r["name"])]

    def get(self, name: str) -> dict[str, Any]:
        """返回整份工况档记录；不存在抛错。"""
        with self._lock:
            records = self._raw_load_locked()
        if name not in records:
            raise CalculationError(
                ERR_PROFILE_NOT_FOUND,
                f"工况档 {name!r} 未登记。可先 GET /profiles 查看已登记工况档。",
                http_status=404,
            )
        return records[name]

    def resolve(self, name: str) -> dict[str, float]:
        """取工况档内的核算参数（float 字典）。"""
        return dict(self.get(name)["params"])

    def create_or_update(self, name: str, params: dict[str, Any], description: str | None = None) -> dict[str, Any]:
        self._validate_name(name)
        clean = self._clean_params(params)
        with self._lock:
            records = self._raw_load_locked()
            now = _utcnow()
            if name in records:
                record = records[name]
                if record.get("builtin"):
                    raise CalculationError(
                        ERR_PROFILE_READONLY,
                        f"内置示范工况 {name!r} 只读，请用别的名字登记自定义工况档。",
                        http_status=409,
                    )
                merged = {**record["params"], **clean}
                self._require_known_fields(merged)
                record["params"] = merged
                if description is not None:
                    record["description"] = description
                record["updated_at"] = now
            else:
                self._require_known_fields(clean)
                record = {
                    "name": name,
                    "description": description or "",
                    "params": clean,
                    "builtin": False,
                    "created_at": now,
                    "updated_at": now,
                }
            records[name] = record
            self._atomic_save_locked(records)
        return self._summarize(record)

    def delete(self, name: str) -> None:
        with self._lock:
            records = self._raw_load_locked()
            if name not in records:
                raise CalculationError(
                    ERR_PROFILE_NOT_FOUND,
                    f"工况档 {name!r} 未登记，无法删除。",
                    http_status=404,
                )
            if records[name].get("builtin"):
                raise CalculationError(
                    ERR_PROFILE_READONLY,
                    f"内置示范工况 {name!r} 只读，不可删除。",
                    http_status=409,
                )
            del records[name]
            self._atomic_save_locked(records)

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _validate_name(name: Any) -> None:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise CalculationError(
                ERR_PROFILE_NAME,
                "工况档名字须为 1-64 个字符，仅允许字母、数字、下划线、连字符、"
                f"点与中文，且不能以点或连字符开头，收到 {name!r}",
                http_status=400,
            )

    @staticmethod
    def _clean_params(params: Any) -> dict[str, float]:
        if not isinstance(params, dict):
            raise CalculationError(
                ERR_PROFILE_INVALID, "params 必须是对象（字段名 -> 数值）。"
            )
        unknown = sorted(set(params) - set(ALL_FIELDS))
        if unknown:
            raise CalculationError(
                ERR_PROFILE_INVALID,
                "params 含未知字段：" + "、".join(unknown)
                + "；允许的字段：" + "、".join(ALL_FIELDS),
            )
        try:
            return coerce_known_fields(params)
        except CalculationError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            raise CalculationError(ERR_PROFILE_INVALID, f"params 无法解析：{exc}") from exc

    @staticmethod
    def _require_known_fields(params: Mapping[str, float]) -> None:
        missing = [f"{n}（{FIELD_LABELS[n]}）" for n in ALL_FIELDS if n not in params]
        if missing:
            raise CalculationError(
                ERR_PROFILE_INVALID,
                "工况档参数不完整，首次登记需给全以下字段，缺失：" + "、".join(missing),
            )

    @staticmethod
    def _summarize(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": record["name"],
            "description": record.get("description", ""),
            "builtin": bool(record.get("builtin", False)),
            "params": dict(record["params"]),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }
