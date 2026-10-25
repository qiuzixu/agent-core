"""蓝绿/灰度发布：确定性用户路由 + 特性开关。

CanaryRouter 基于用户 ID 的确定性哈希（MD5），把请求分配到：
- ``blue``（稳定版，默认）
- ``green``（实验版，按百分比灰度）

同一用户始终命中同一组（粘性路由），适合 A/B 测试和新 Prompt 逐步放量。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CanaryConfig:
    """灰度发布配置。"""

    enabled: bool = False
    # 灰度百分比（0-100）：多少比例的用户路由到 green。
    percentage: int = 0
    # 特性开关：flag_name -> 是否对 green 组启用。
    feature_flags: dict[str, bool] | None = None
    # 强制指定路由（调试用）：覆盖哈希结果。
    forced_route: str | None = None


class CanaryRouter:
    priority = 6
    """确定性灰度路由器。"""

    def __init__(self, config: CanaryConfig | None = None) -> None:
        self._config = config or CanaryConfig()

    def route(self, user_id: str) -> str:
        """返回 "blue" 或 "green"（同一 user 始终命中同一组）。"""
        if not self._config.enabled:
            return "blue"
        if self._config.forced_route:
            return self._config.forced_route
        if self._config.percentage <= 0:
            return "blue"
        if self._config.percentage >= 100:
            return "green"
        digest = hashlib.md5(user_id.encode()).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return "green" if bucket < self._config.percentage else "blue"

    def is_feature_enabled(self, flag: str, user_id: str) -> bool:
        """特性开关：green 组按 feature_flags 决定，blue 组始终 False。"""
        if self.route(user_id) != "green":
            return False
        return bool(self._config.feature_flags and self._config.feature_flags.get(flag, False))


__all__ = ["CanaryConfig", "CanaryRouter"]
