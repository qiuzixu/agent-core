"""ApolloConfigCenter 单元测试（LangGraph 端，urllib 实现无额外依赖）。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch


def _make_mock_urlopen(configs_iter):
    """返回一个 patch urlopen 的 side_effect 函数，响应为 context manager。"""

    def side_effect(req, **kwargs):
        cfg = next(configs_iter)
        body = json.dumps({
            "appId": "test-app", "cluster": "default",
            "namespaceName": "application",
            "configurations": cfg, "releaseKey": "rk-001",
        }).encode()

        @contextmanager
        def _resp():
            yield type("R", (), {"read": lambda s: body})()

        return _resp()

    return side_effect


class TestApolloConfigCenter:
    def _make_center(self, configs, poll_seconds=3600):
        from low_altitude_agent.platform_middleware import ApolloConfigCenter
        it = iter(configs if isinstance(configs, list) else [configs])
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(it)):
            cc = ApolloConfigCenter(
                server_url="http://mock:8080", app_id="test-app",
                namespace="application", poll_seconds=poll_seconds, timeout_seconds=2,
            )
        return cc, it

    def test_get_existing_key(self):
        cc, _ = self._make_center({"llm.temperature": "0.7", "max_tokens": "2048"})
        assert cc.get("llm.temperature") == "0.7"
        assert cc.get("max_tokens") == "2048"

    def test_get_missing_key_returns_default(self):
        cc, _ = self._make_center({"a": "1"})
        assert cc.get("b", default="fallback") == "fallback"
        assert cc.get("c") is None

    def test_data_snapshot(self):
        cc, _ = self._make_center({"k1": "v1", "k2": "v2"})
        assert cc.data == {"k1": "v1", "k2": "v2"}

    def test_on_change_callback_fires_on_config_change(self):
        from low_altitude_agent.platform_middleware import ApolloConfigCenter

        changes = []
        configs_seq = [{"old_key": "old_val"}, {"new_key": "new_val"}]
        it = iter(configs_seq)
        with patch("urllib.request.urlopen", side_effect=_make_mock_urlopen(it)):
            cc = ApolloConfigCenter(
                server_url="http://mock:8080", app_id="test-app",
                namespace="application", poll_seconds=0.05, timeout_seconds=2,
            )
            cc.on_change(lambda snapshot: changes.append(snapshot))
            cc._last_fetch = 0  # force re-poll
            cc.data  # triggers _check_poll → _fetch_sync
        assert len(changes) > 0
        assert changes[-1] == {"new_key": "new_val"}

    def test_apollo_unreachable_uses_local_cache(self):
        from low_altitude_agent.platform_middleware import ApolloConfigCenter
        import time

        call_count = 0

        def side_effect(req, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                body = json.dumps({
                    "appId": "test-app", "cluster": "default",
                    "namespaceName": "application",
                    "configurations": {"cached": "yes"}, "releaseKey": "rk-001",
                }).encode()

                @contextmanager
                def _resp():
                    yield type("R", (), {"read": lambda s: body})()

                return _resp()
            raise ConnectionError("Apollo down")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            cc = ApolloConfigCenter(
                server_url="http://mock:8080", app_id="test-app",
                namespace="application", poll_seconds=0.05, timeout_seconds=2,
            )
        # Now Apollo is down, should use cached data
        cc._last_fetch = 0  # force re-poll
        cc.data
        assert cc.get("cached") == "yes"
