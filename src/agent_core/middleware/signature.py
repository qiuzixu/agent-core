"""请求签名 / 防重放中间件。

防止中间人篡改请求内容和截获后原样重发。

协议：
  客户端计算：
    timestamp = 当前 Unix 秒
    nonce     = 一次性随机字符串（UUID）
    body_hash = SHA256(请求体字节) 的 hex
    signature = HMAC-SHA256(secret, timestamp + nonce + method + path + body_hash) 的 hex

  请求头：
    X-Timestamp: <timestamp>
    X-Nonce: <nonce>
    X-Signature: <signature>

  服务端验证：
    1. timestamp 距当前时间偏移 <= max_age_seconds（防重放窗口）
    2. nonce 未被使用过（内存去重，TTL 内）
    3. 重算 HMAC 签名并 hmac.compare_digest 比对（防篡改）

零依赖实现（纯标准库 hmac / hashlib / time / uuid）。

配置：
    AGENT_SIGNATURE_ENABLED: 是否启用（默认 false，需要前端配合）
    AGENT_SIGNATURE_SECRET: 共享密钥（前后端必须一致）
    AGENT_SIGNATURE_MAX_AGE_SECONDS: 时间戳偏移上限（默认 300s）
    AGENT_SIGNATURE_NONCE_TTL_SECONDS: nonce 去重记录保留时长（默认 600s）
    AGENT_SIGNATURE_PUBLIC_PATHS: 免签名路径前缀（与认证共用默认值）
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = [
    "SignatureConfig",
    "NonceStore",
    "compute_signature",
    "verify_signature",
    "SignatureMiddleware",
]

_DEFAULT_PUBLIC_PATHS = ["/health", "/ok", "/docs", "/openapi.json"]


@dataclass
class SignatureConfig:
    """签名中间件配置。"""

    enabled: bool = False
    secret: str = ""
    max_age_seconds: float = 300.0
    nonce_ttl_seconds: float = 600.0
    public_paths: list[str] = field(default_factory=lambda: list(_DEFAULT_PUBLIC_PATHS))


class NonceStore:
    """内存 nonce 去重存储。

    每个 nonce 在 TTL 内只允许出现一次，第二次出现视为重放。
    超过 TTL 的条目自动清理，防止内存泄漏。
    """

    def __init__(self, ttl_seconds: float = 600.0, max_entries: int = 100_000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._seen: dict[str, float] = {}  # {nonce: timestamp}

    def check_and_add(self, nonce: str) -> bool:
        """检查 nonce 是否已使用。返回 True 表示是新的（通过），False 表示重复（拒绝）。"""
        now = time.monotonic()
        self._cleanup(now)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        # 防止极端情况内存溢出
        if len(self._seen) > self._max_entries:
            oldest = sorted(self._seen.items(), key=lambda x: x[1])[: len(self._seen) // 2]
            for k, _ in oldest:
                del self._seen[k]
        return True

    def _cleanup(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self._ttl]
        for k in expired:
            del self._seen[k]


def generate_nonce() -> str:
    """生成一次性随机 nonce。"""
    return uuid.uuid4().hex


def compute_signature(
    secret: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes | None = None,
) -> str:
    """计算 HMAC-SHA256 签名。

    签名串 = timestamp + nonce + METHOD + path + SHA256(body)
    """
    body_hex = hashlib.sha256(body or b"").hexdigest()
    message = f"{timestamp}{nonce}{method.upper()}{path}{body_hex}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_signature(
    config: SignatureConfig,
    nonce_store: NonceStore,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[bool, str | None]:
    """验证请求签名。

    Returns:
        (是否通过, 错误信息)
    """
    if not config.enabled:
        return True, None

    # 公开路径跳过
    for prefix in config.public_paths:
        if path.startswith(prefix):
            return True, None

    if not config.secret:
        return False, "服务端未配置签名密钥"

    ts = headers.get("x-timestamp", "")
    nonce = headers.get("x-nonce", "")
    sig = headers.get("x-signature", "")

    if not ts or not nonce or not sig:
        return False, "缺少签名请求头（X-Timestamp / X-Nonce / X-Signature）"

    # 1. 时间戳校验
    try:
        ts_val = float(ts)
    except ValueError:
        return False, "时间戳格式错误"
    offset = abs(time.time() - ts_val)
    if offset > config.max_age_seconds:
        return False, f"时间戳偏移过大（{offset:.0f}s > {config.max_age_seconds:.0f}s），疑似重放"

    # 2. nonce 去重
    if not nonce_store.check_and_add(nonce):
        return False, "nonce 已使用，疑似重放请求"

    # 3. HMAC 验签
    expected = compute_signature(config.secret, ts, nonce, method, path, body)
    if not hmac.compare_digest(expected, sig):
        return False, "签名不匹配，请求可能被篡改"

    return True, None


class SignatureMiddleware:
    """Agent 层签名验证（供 HTTP 框架适配层调用）。

    HTTP 层通过 verify_signature() 直接调用，
    此类提供统一的配置持有 + NonceStore 管理。
    """

    def __init__(self, config: SignatureConfig | None = None) -> None:
        self._config = config or SignatureConfig()
        self._nonce_store = NonceStore(ttl_seconds=self._config.nonce_ttl_seconds)

    @property
    def config(self) -> SignatureConfig:
        return self._config

    @property
    def nonce_store(self) -> NonceStore:
        return self._nonce_store

    def verify(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[bool, str | None]:
        """便捷方法：使用内部 NonceStore 验证签名。"""
        return verify_signature(self._config, self._nonce_store, method, path, headers, body)