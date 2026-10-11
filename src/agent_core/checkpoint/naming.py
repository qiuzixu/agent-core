"""checkpoint 文件名清洗与原子写入的底层工具。

职责：
- thread_id 与文件名之间的严格双向映射（`_sanitize_thread_id` / `_restore_thread_id`）；
- 相关常量（`_FILENAME_*` / `_WINDOWS_RESERVED` / `_HEX_ESCAPE`）；
- JSON 文件的临时文件 + 原子替换写入（`_atomic_write_json`）。

本模块不依赖包内其他模块，供 `store` 与 `travel` 复用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# 文件名严格清洗：白名单字符原样保留（可读性），其余按 UTF-8 字节转义为 %XX。
# Windows 非法字符 <>:"/\|?*、``%``（转义前缀，必须一并转义保证可逆）和控制符都要处理。
_FILENAME_ILLEGAL_CHARS = frozenset('<>:"/\\|?*%')
_FILENAME_ESCAPE_CHARS = _FILENAME_ILLEGAL_CHARS | {chr(code) for code in range(0x20)}
# Windows 保留设备名（不区分大小写，且无论后缀如何都保留）：只检查文件名第一个点之前的主干。
_WINDOWS_RESERVED = re.compile(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", re.IGNORECASE)
_HEX_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _escape_char(char: str) -> str:
    """把单个字符转义为 UTF-8 字节的 %XX 序列。"""
    return "".join(f"%{byte:02X}" for byte in char.encode("utf-8", errors="surrogatepass"))


def _sanitize_thread_id(thread_id: str) -> str:
    """把 thread_id 清洗为跨平台安全且可逆的文件名主干。

    规则：
    - 白名单（可打印且不属于非法字符集）原样保留，保证可读性；
    - 其余字符（Windows 非法字符 ``<>:"/\\|?*``、``%``、控制符、孤立代理对等）
      按 UTF-8 字节转义为 ``%XX``。``%`` 本身也被转义，因此清洗是单射：
      ``a/b`` 与 ``a_b`` 会得到不同文件名，不再互相覆盖；
    - 以点/空格结尾或命中 Windows 保留设备名（CON、PRN、COM1 等）时，
      对首个点之前主干的末字符转义规避系统特殊处理，映射仍然可逆。
    """
    sanitized = "".join(
        char if char.isprintable() and char not in _FILENAME_ESCAPE_CHARS else _escape_char(char)
        for char in thread_id
    )
    if sanitized.endswith((".", " ")):
        sanitized = sanitized[:-1] + _escape_char(sanitized[-1])
    base_name = sanitized.split(".", 1)[0]
    if _WINDOWS_RESERVED.fullmatch(base_name):
        sanitized = sanitized[:-1] + _escape_char(sanitized[-1])
    return sanitized


def _restore_thread_id(file_stem: str) -> str:
    """`_sanitize_thread_id` 的逆映射；无法还原的历史文件名原样返回。"""
    if "%" not in file_stem:
        return file_stem
    raw = bytearray()
    index = 0
    while index < len(file_stem):
        match = _HEX_ESCAPE.match(file_stem, index)
        if match is None:
            raw.extend(file_stem[index].encode("utf-8", errors="surrogatepass"))
            index += 1
        else:
            raw.append(int(match.group(1), 16))
            index = match.end()
    return raw.decode("utf-8", errors="surrogatepass")


def _atomic_write_json(file_path: Path, value: Any) -> None:
    """在目标目录写临时文件并原子替换，避免崩溃留下截断 JSON。"""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, file_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
