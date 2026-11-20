"""An edit to an already-answered Slack message, arriving after a restart, posts nothing.

Real GatewayRunner + SessionAuthority + SQLite across two owner epochs (core's own redelivery
harness); only the transport is stubbed. Run from a hermes-agent checkout (see README).
"""

import asyncio
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("tests.gateway.test_command_bypass_active_session")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MESSAGE_ID = "1700000000.000700"


class _Ctx:
    def register_hook(self, name, cb):
        pass

    def on_unload(self, cb):
        pass


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "slack_multiplayer_edit_conflict_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(_Ctx())


_load_plugin()


async def _gateway(tmp_path, monkeypatch, platform_name):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_ingress_context import register_transport_home
    from tests.gateway.test_command_bypass_active_session import _StubAdapter
    platform = Platform(platform_name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(f"{platform_name.upper()}_ALLOWED_USERS", "U42")
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False, sessions_dir=tmp_path / "sessions"))
    adapter = _StubAdapter(PlatformConfig(enabled=True, token="fixture", typing_indicator=False), platform)
    sent, calls = [], []

    async def send(chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True, message_id=str(len(sent)))
    monkeypatch.setattr(adapter, "send", send)

    async def run_agent(**kwargs):
        calls.append(kwargs.get("message"))
        return {"final_response": "the answer", "messages": [], "tools": [], "history_offset": 0,
                "last_prompt_tokens": 0}
    runner._run_agent = run_agent
    adapter.gateway_runner = runner
    runner.adapters = {platform: adapter}
    register_transport_home(runner, None, tmp_path)
    return runner, adapter, sent, calls


async def _settled(adapter, want):
    for _ in range(200):
        await asyncio.sleep(0.05)
        if not adapter._background_tasks and not adapter._session_tasks and want():
            break
    await asyncio.sleep(0.2)


async def _edit_after_restart(tmp_path, monkeypatch, platform_name):
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import recover_profile_native_sessions
    from gateway.session_authority import initialize_session_authority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    runner, adapter, sent, calls = await _gateway(tmp_path, monkeypatch, platform_name)
    source = adapter.build_source(chat_id="C0000000001", chat_type="group", user_id="U42")
    try:
        first = await initialize_session_authority(runner, profile_id=str(get_hermes_home()),
                                                   instance_id="before-restart")
        first._schedule = lambda ref: None
        original = MessageEvent(text="original text", source=source, message_id=MESSAGE_ID,
                                timestamp=datetime.now() - timedelta(hours=8))
        with native_callback(runner, original, get_hermes_home()):
            await first.admit_native(original)
        second = await initialize_session_authority(runner, profile_id=str(get_hermes_home()),
                                                    instance_id="after-restart")
        await recover_profile_native_sessions(runner, second)
        runner._wire_adapter_handlers(adapter)
        await _settled(adapter, lambda: "the answer" in sent)
        # Slack's message_changed: same ts, edited text.
        await adapter.handle_message(MessageEvent(text="original text, edited", source=source,
                                                  message_id=MESSAGE_ID))
        await _settled(adapter, lambda: False)
    finally:
        runner.session_store.close_all_db_handles()
    return sent, calls


@pytest.mark.asyncio
async def test_slack_edit_of_an_answered_message_after_restart_posts_nothing(tmp_path, monkeypatch):
    sent, calls = await _edit_after_restart(tmp_path, monkeypatch, "slack")
    assert calls == ["original text"], "the original runs once and the edit never runs"
    replies = [t for t in sent if "home channel" not in t]
    assert replies == ["the answer"], sent


@pytest.mark.asyncio
async def test_other_platforms_still_get_the_conflict_notice(tmp_path, monkeypatch):
    sent, calls = await _edit_after_restart(tmp_path, monkeypatch, "telegram")
    assert calls == ["original text"]
    assert any("already received" in t for t in sent), sent
