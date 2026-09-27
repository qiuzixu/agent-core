"""安全、版本化的 Core 对象序列化。"""

from agent_core.serialization.registry import (
    SerializedEnvelope,
    SerializerRegistry,
    default_serializer_registry,
    dumpd,
    dumps,
    load,
    loads,
)

__all__ = [
    "SerializedEnvelope",
    "SerializerRegistry",
    "default_serializer_registry",
    "dumpd",
    "dumps",
    "load",
    "loads",
]
