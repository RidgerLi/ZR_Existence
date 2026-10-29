"""
本地能力插件（Capabilities）
Author: ZerolanLiveRobot

把"不依赖大模型、确定性的本地小功能"做成可插拔的 .py 文件，放在固定目录：

    capabilities/
        time_now.py
        note.py
        my_skill.py      ← 你新加的，保存即被扫描到（目录 mtime 变化会自动重载）

单个文件的最小约定（三个模块级名字 + 一个函数）：

    NAME = "time_now"                      # 唯一名字，也是 /api/capability.run 的参数
    DESCRIPTION = "返回当前本地时间"        # 显示在面板上的说明
    def run(args: dict) -> str:            # args 来自 UI/HTTP；返回值会展示并记日志
        return "2026-10-29 11:00"

管理能力：
    - 开关状态持久化到 resources/capabilities.json；
    - 每次调用记录耗时、成功与否、结果摘要（SQLite capability_runs）；
    - 调用会写一条 `[CAP] name ok=… ms=…` 的日志，因此在面板日志里可以按 CAP 分类筛出来。

能力只能被"显式调用"（面板按钮 / HTTP / 控制台），不会被大模型自动触发——这是刻意的：
让新增能力始终是你审过的、可预期的东西。
"""

import importlib.util
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from framework import telemetry


@dataclass
class Capability:
    name: str
    description: str
    path: str
    run: Any  # Callable[[dict], Any]
    module: Any = None


class CapabilityManager:
    def __init__(self, directory: str, state_path: str):
        self._dir = Path(directory)
        self._state_path = Path(state_path)
        self._lock = threading.RLock()
        self._caps: Dict[str, Capability] = {}
        self._enabled: Dict[str, bool] = {}
        self._dir_mtime: Optional[float] = None
        self._load_state()
        self.reload()

    # ---- 持久化开关 ----
    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._enabled = {str(k): bool(v) for k, v in (data.get("enabled") or {}).items()}
        except Exception as e:
            logger.warning(f"Failed to load capability state: {e}")

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"enabled": self._enabled}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save capability state: {e}")

    # ---- 扫描 / 加载 ----
    def _dir_stamp(self) -> Optional[float]:
        try:
            return self._dir.stat().st_mtime
        except OSError:
            return None

    def reload(self) -> List[Capability]:
        with self._lock:
            self._dir_mtime = self._dir_stamp()
            caps: Dict[str, Capability] = {}
            if self._dir.exists():
                for path in sorted(self._dir.glob("*.py")):
                    if path.name.startswith("_"):
                        continue
                    cap = self._load_one(path)
                    if cap is not None:
                        caps[cap.name] = cap
            self._caps = caps
            logger.info(f"Capabilities loaded: {sorted(caps)}")
            return list(caps.values())

    @staticmethod
    def _load_one(path: Path) -> Optional[Capability]:
        try:
            spec = importlib.util.spec_from_file_location(f"zrl_capability_{path.stem}", path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            name = str(getattr(module, "NAME", path.stem)).strip()
            run = getattr(module, "run", None)
            if not name or not callable(run):
                logger.warning(f"Capability {path.name} ignored: missing NAME or run(args)")
                return None
            return Capability(name=name, description=str(getattr(module, "DESCRIPTION", "")).strip(),
                              path=str(path), run=run, module=module)
        except Exception as e:
            logger.warning(f"Failed to load capability {path.name}: {e}")
            return None

    def maybe_reload(self) -> None:
        """目录新增/删除 .py 后自动重载（不用重启）。"""
        if self._dir_stamp() != self._dir_mtime:
            self.reload()

    # ---- 查询 ----
    def is_enabled(self, name: str) -> bool:
        return bool(self._enabled.get(name, True))

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._caps)

    def list_info(self) -> List[Dict[str, Any]]:
        self.maybe_reload()
        stats = {r["name"]: r for r in telemetry.capability_stats()}
        out = []
        with self._lock:
            for name, cap in sorted(self._caps.items()):
                s = stats.get(name, {})
                out.append({
                    "name": name,
                    "description": cap.description,
                    "enabled": self.is_enabled(name),
                    "runs": int(s.get("runs") or 0),
                    "ok_runs": int(s.get("ok_runs") or 0),
                    "avg_ms": round(float(s.get("avg_ms") or 0), 1),
                    "last_ts": s.get("last_ts"),
                })
        return out

    # ---- 控制 ----
    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            if name not in self._caps:
                raise KeyError(f"unknown capability: {name}")
            self._enabled[name] = bool(enabled)
            self._save_state()
        logger.info(f"[CAP] {'enabled' if enabled else 'disabled'}: {name}")
        return bool(enabled)

    def run(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.maybe_reload()
        with self._lock:
            cap = self._caps.get(name)
        if cap is None:
            raise KeyError(f"unknown capability: {name}")
        if not self.is_enabled(name):
            raise PermissionError(f"capability disabled: {name}")
        args = args or {}
        t0 = time.monotonic()
        ok = True
        try:
            result = cap.run(args)
            detail = str(result)
            logger.info(f"[CAP] {name} ok ms={int((time.monotonic() - t0) * 1000)} result={detail[:200]}")
            return {"name": name, "result": result}
        except Exception as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"
            logger.warning(f"[CAP] {name} failed: {detail}")
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            try:
                telemetry.record_capability_run(name, ok, ms, detail)
            except Exception as e:
                logger.debug(f"Capability run not recorded: {e}")

    def recent_runs(self, limit: int = 20, name: Optional[str] = None) -> List[Dict[str, Any]]:
        return telemetry.recent_capability_runs(limit=limit, name=name)
