"""JWT / Token 认证中间件。

验证请求中的 Authorization 头或 x-api-key 头，
支持 JWT（HMAC-SHA256）和静态 API Key 两种模式。
认证失败时返回 STOP，短路后续中间件和业务逻辑。

配置：
    AGENT_AUTH_MODE: none | api_key | jwt （默认 none，向后兼容）
    AGENT_AUTH_API_KEYS: 逗号分隔的合法 key 列表
    AGENT_AUTH_JWT_SECRET: JWT 签名密钥
    AGENT_AUTH_JWT_ALGORITHM: 签名算法（默认 HS256）
    AGENT_AUTH_PUBLIC_PATHS: 逗号分隔的免认证路径前缀（如 /health,/ok,/docs）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["AuthConfig", "AuthMiddleware"]


# ── JWT 轻量实现（不引入 PyJWT，保持零依赖）──────────────────


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = 4 - len(data) % 4
    return base64.urlsafe_b64decode(data + "=" * padding)


def jwt_encode(payload: dict[str, Any], secret: str, algorithm: str = "HS256") -> str:
    """生成 JWT 令牌。"""
    header = {"alg": algorithm, "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    message = f"{header_b64}.{payload_b64}"
    if algorithm == "HS256":
        sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    else:
        raise ValueError(f"不支持的签名算法: {algorithm}")
    return f"{message}.{_b64url_encode(sig)}"


def jwt_decode(token: str, secret: str, algorithm: str = "HS256") -> dict[str, Any] | None:
    """验证并解析 JWT 令牌。返回 payload 或 None。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != algorithm:
            return None
        message = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        # 过期检查
        if "exp" in payload and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


# ── 配置 ────────────────────────────────────────────────────


class AuthMode(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    JWT = "jwt"


@dataclass
class AuthConfig:
    """认证中间件配置。"""

    mode: AuthMode = AuthMode.NONE
    api_keys: set[str] = field(default_factory=set)
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # 免认证路径前缀
    public_paths: list[str] = field(
        default_factory=lambda: ["/health", "/ok", "/docs", "/openapi.json"]
    )
    # Token 有效期（秒），仅用于生成
    token_ttl_seconds: int = 86400  # 24h


# ── 中间件 ─────────────────────────────────────────────────


class AuthMiddleware(Middleware):
    """认证中间件：验证请求身份，失败时短路。

    用法：
        config = AuthConfig(
            mode=AuthMode.JWT,
            jwt_secret="your-secret-key",
            api_keys={"key-1", "key-2"},
        )
        middleware = AuthMiddleware(config=config)
    """

    def __init__(self, config: AuthConfig | None = None) -> None:
        self._config = config or AuthConfig()

    def verify_request(
        self,
        path: str,
        headers: dict[str, str],
    ) -> tuple[bool, str | None, dict[str, Any]]:
        """验证 HTTP 请求身份。

        Returns:
            (是否通过, 错误信息, 解析出的用户信息)
        """
        cfg = self._config
        if cfg.mode == AuthMode.NONE:
            return True, None, {}

        # 公开路径跳过
        for prefix in cfg.public_paths:
            if path.startswith(prefix):
                return True, None, {}

        # 从 headers 提取令牌
        auth_header = headers.get("authorization", "")
        api_key_header = headers.get("x-api-key", "")

        if cfg.mode == AuthMode.API_KEY:
            key = api_key_header or auth_header.replace("Bearer ", "") if auth_header else api_key_header
            if not key:
                return False, "缺少 x-api-key 头", {}
            if key not in cfg.api_keys:
                return False, "无效的 API Key", {}
            return True, None, {"api_key_id": key[:8] + "..."}

        if cfg.mode == AuthMode.JWT:
            token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""
            if not token:
                return False, "缺少 Authorization: Bearer <token>", {}
            payload = jwt_decode(token, cfg.jwt_secret, cfg.jwt_algorithm)
            if payload is None:
                return False, "JWT 无效或已过期", {}
            return True, None, payload

        return False, f"未知认证模式: {cfg.mode}", {}

    # ── Agent Middleware 钩子（用于非 HTTP 调用场景）──────────

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        """在 Agent 层验证 metadata 中携带的认证信息。"""
        auth_meta = context.metadata.get("auth")
        if not auth_meta:
            # 没有 auth 信息时，如果 mode=none 就放行
            if self._config.mode == AuthMode.NONE:
                return MiddlewareResult(action=MiddlewareAction.CONTINUE)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error="缺少认证信息",
            )
        # 已经在 HTTP 层验证过，这里只检查是否携带了用户身份
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
