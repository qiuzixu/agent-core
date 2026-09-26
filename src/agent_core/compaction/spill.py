"""外置存档（spill）：把被瘦身的完整工具输出存到进程内 KV 存。

瘦身只保留头尾，中间内容被截断。如果模型后续需要完整内容（例如用户
追问"刚才那个航线列表的第三条详情"），可以通过 ``read_spilled_content``
工具按 spill_id 回读全文，而不是重新调一次业务接口。

存储是进程内的、按会话隔离的；进程重启后 spill 数据丢失——这在低空
Agent 场景下可接受：工具输出本就是实时数据，过期后重查比回读旧快照
更可靠。如果将来需要持久化，把 ``_store`` 换成 Redis / SQLite 即可，
接口不变。
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from threading import Lock


class SpillStore:
    """线程安全的进程内 spill 存储（LRU 淘汰，防无界增长）。

    用法:
        store = SpillStore()
        spill_id = store.put(big_json_string)
        # … pruner 把 spill_id 写进瘦身标记 …
        content = store.get(spill_id)   # 模型通过工具回读
    """

    def __init__(self, *, max_entries: int = 256, max_chars_per_entry: int = 512 * 1024) -> None:
        """Args:
        max_entries: 最多保存多少条 spill；超出 LRU 淘汰最旧的。
        max_chars_per_entry: 单条 spill 上限（字符），防止一条巨型
            输出吃掉所有内存；超限的条目不存、只截断标记里不出现 ID。
        """
        if max_entries <= 0:
            raise ValueError("max_entries 必须为正整数")
        if max_chars_per_entry <= 0:
            raise ValueError("max_chars_per_entry 必须为正整数")
        self._store: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()
        self._max_entries = max_entries
        self._max_chars = max_chars_per_entry

    def put(self, content: str) -> str | None:
        """存入完整内容，返回 spill_id；内容超限时返回 None（不存）。

        Args:
            content: 被瘦身的完整工具输出原文。

        Returns:
            可用于回读的 spill_id；若内容超过 ``max_chars_per_entry``
            则返回 None，调用方应回退到纯截断标记。
        """
        if not content or len(content) > self._max_chars:
            return None
        spill_id = f"spill-{uuid.uuid4().hex[:12]}"
        with self._lock:
            # LRU：已满则弹出最旧条目
            while len(self._store) >= self._max_entries:
                self._store.popitem(last=False)
            self._store[spill_id] = content
        return spill_id

    def get(self, spill_id: str) -> str | None:
        """按 spill_id 回读完整内容；不存在时返回 None。"""
        with self._lock:
            content = self._store.get(spill_id)
            if content is not None:
                # 命中后移到末尾（LRU 最近使用）
                self._store.move_to_end(spill_id)
            return content

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        """清空所有 spill 条目。"""
        with self._lock:
            self._store.clear()
