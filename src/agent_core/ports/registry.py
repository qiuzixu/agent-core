"""服务注册中心端口。

定义服务注册与发现的最小协议，供未来接入 Nacos / Consul / etcd 等
注册中心实现。当前仅声明协议，业务层按需实现具体适配器。

典型用法（以 Nacos 为例）：
    class NacosServiceRegistry:
        async def register(self, service) -> None:
            # 调 Nacos OpenAPI 注册实例
            ...
        async def deregister(self, service_name, instance_id) -> None:
            ...
        async def discover(self, service_name) -> list[ServiceInstance]:
            # 按 Nacos 集群/分组查询健康实例
            ...
        async def heartbeat(self, service_name, instance_id) -> None:
            # 发送心跳保活
            ...

多 Agent / 多实例部署时才真正需要注册中心：
    - Agent A 要调用 Agent B 的 HTTP 接口，但 B 的地址动态变化；
    - 多个 worker 实例需要被调度器发现并分配任务；
    - 需要按权重做负载均衡。
单实例开发模式下不需要，enabled=False 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["ServiceInstance", "ServiceRegistry"]


@dataclass
class ServiceInstance:
    """一个已注册的服务实例。

    Attributes:
        service_name: 服务名，如 "my-cesium-agent-vanilla"。
        instance_id:  实例唯一标识，如 "node-1" 或 UUID。
        host:         可访问的 IP / 主机名。
        port:         监听端口。
        healthy:      是否健康（心跳超时会被标记为 False）。
        weight:       负载均衡权重，默认 1。
        metadata:     附加元数据，如 {"version": "1.2.0", "region": "cn-north"}。
    """

    service_name: str
    instance_id: str
    host: str
    port: int
    healthy: bool = True
    weight: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        """返回 http://host:port 格式的完整地址。"""
        return f"http://{self.host}:{self.port}"


@runtime_checkable
class ServiceRegistry(Protocol):
    """服务注册中心协议（Nacos / Consul / etcd 等）。"""

    async def register(self, instance: ServiceInstance) -> None:
        """注册一个服务实例（临时节点，心跳超时自动摘除）。"""
        ...

    async def deregister(self, service_name: str, instance_id: str) -> None:
        """主动注销一个服务实例（优雅下线时调用）。"""
        ...

    async def discover(self, service_name: str) -> list[ServiceInstance]:
        """按服务名查询所有健康实例列表。"""
        ...

    async def heartbeat(self, service_name: str, instance_id: str) -> None:
        """发送心跳，续约临时实例（定期调用，防止被摘除）。"""
        ...

    async def close(self) -> None:
        """关闭注册中心连接（应用退出时调用）。"""
        ...
