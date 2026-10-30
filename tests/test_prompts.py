"""PromptRegistry 单元测试：版本管理、热重载、render_messages。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_core.prompts import PromptRegistry
from agent_core.protocol.messages import Message


class TestPromptRegistryBasics:
    """基础版本管理功能。"""

    def test_register_and_render(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("greet", "你好 {name}", description="打招呼")
        assert registry.render("greet", name="张三") == "你好 张三"

    def test_update_creates_new_version(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("greet", "你好 v1")
        registry.update("greet", "你好 v2")
        assert registry.render("greet") == "你好 v2"
        assert len(registry.get_history("greet")) == 2

    def test_rollback(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("greet", "你好 v1")
        registry.update("greet", "你好 v2")
        registry.rollback("greet", 1)
        assert registry.render("greet") == "你好 v1"

    def test_safe_formatter_rejects_attribute_access(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        with pytest.raises(ValueError, match="只允许简单变量名"):
            registry.register("bad", "{obj.attr}")


class TestPromptHotReload:
    """热重载：外部修改 prompts.json 后无需重启。"""

    def test_check_for_changes_detects_mtime(self, tmp_path: Path) -> None:
        path = tmp_path / "prompts.json"
        registry = PromptRegistry(path)
        registry.register("greet", "v1")
        assert not registry.check_for_changes()
        # 模拟外部修改：直接写文件
        time.sleep(0.05)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["greet"]["current_version"] = 1  # 不变，只触发 mtime
        path.write_text(json.dumps(data), encoding="utf-8")
        assert registry.check_for_changes()

    def test_reload_changed_picks_up_external_changes(self, tmp_path: Path) -> None:
        path = tmp_path / "prompts.json"
        registry = PromptRegistry(path)
        registry.register("greet", "你好 v1")
        # 外部修改：注册新提示词
        time.sleep(0.05)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["new_prompt"] = {
            "name": "new_prompt",
            "current_version": 1,
            "versions": [
                {"version": 1, "content": "新提示词", "description": "", "defaults": {}, "created_at": ""}
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        assert registry.check_for_changes()
        count = registry.reload_changed()
        assert count == 2  # greet + new_prompt
        assert "new_prompt" in registry.list_prompts()

    def test_reload_changed_no_change_returns_zero(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("greet", "v1")
        assert registry.reload_changed() == 0

    def test_reload_corrupt_file_keeps_memory(self, tmp_path: Path) -> None:
        path = tmp_path / "prompts.json"
        registry = PromptRegistry(path)
        registry.register("greet", "v1")
        # 写入损坏 JSON
        time.sleep(0.05)
        path.write_text("{broken", encoding="utf-8")
        assert registry.check_for_changes()
        count = registry.reload_changed()
        assert count == 0  # 失败返回 0
        assert "greet" in registry.list_prompts()  # 内存数据保留


class TestRenderMessages:
    """render_messages：把模板渲染为消息列表。"""

    def test_render_system_only(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("sys", "你是 {role}")
        messages = registry.render_messages("sys", system_variables={"role": "助手"})
        assert len(messages) == 1
        assert messages[0].role == "system"
        assert messages[0].content == "你是 助手"

    def test_render_with_history_and_user(self, tmp_path: Path) -> None:
        registry = PromptRegistry(tmp_path / "prompts.json")
        registry.register("sys", "你是助手")
        history = [Message(role="user", content="旧问题"), Message(role="assistant", content="旧回答")]
        messages = registry.render_messages(
            "sys",
            system_variables={},
            history=history,
            user_variables={"question": "新问题"},
        )
        assert len(messages) == 4  # system + 2 history + user
        assert messages[0].role == "system"
        assert messages[-1].role == "user"
        assert "新问题" in messages[-1].content
