"""动态配置中心中间件。

依赖 ports.ConfigCenter 协议读取配置；agent-core 内置 JSON 文件实现，
业务层可注入 Apollo / Consul / Nacos / etcd 适配器。

优先级：ConfigCenter.data 中的值 > 环境变量 > 代码默认值。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from agent_core.ports.platform import ConfigCenter

logger = logging.getLogger(__name__)

__all__ = ["FileConfigCenter"]


class FileConfigCenter:
    """内置默认配置中心：JSON 文件 + mtime 轮询热更新。

    业务层如果要接 Apollo / Consul，实现 ConfigCenter 协议即可。
    """

    def __init__(
        self,
        path: str = "./config_dynamic.json",
        poll_seconds: float = 5.0,
        enabled: bool = False,
    ) -> None:
        self._path = path
        self._poll_seconds = poll_seconds
        self._enabled = enabled
        self._data: dict[str, Any] = {}
        self._last_mtime: float = 0.0
        self._last_check: float = 0.0
        self._lock = threading.Lock()
        self._callbacks: list = []
        if enabled:
            self._load()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def data(self) -> dict[str, Any]:
        self._check_reload()
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        self._check_reload()
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._write()
        self._notify_change()

    def on_change(self, callback: Any) -> None:
        """注册配置变更回调。"""
        self._callbacks.append(callback)

    def _notify_change(self) -> None:
        for cb in self._callbacks:
            try:
                cb(dict(self._data))
            except Exception:
                logger.exception("[DynamicConfig] 变更回调执行失败")

    def _load(self) -> None:
        p = Path(self._path)
        if not p.exists():
            return
        try:
            with open(p, encoding="utf-8") as f:
                self._data = json.load(f)
            self._last_mtime = p.stat().st_mtime
            logger.info("[DynamicConfig] 加载配置文件: %s (%d 项)", self._path, len(self._data))
        except Exception:
            logger.exception("[DynamicConfig] 加载配置文件失败: %s", self._path)

    def _check_reload(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        if now - self._last_check < self._poll_seconds:
            return
        self._last_check = now
        p = Path(self._path)
        if not p.exists():
            return
        mtime = p.stat().st_mtime
        if mtime > self._last_mtime:
            old = dict(self._data)
            self._load()
            if self._data != old:
                self._notify_change()

    def _write(self) -> None:
        try:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("[DynamicConfig] 写回失败")
