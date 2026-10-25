"""Gateway patches: silence markers stay silent, and steered messages in shared chats name
their sender.

Run from a hermes-agent checkout with its dev environment (see README).
"""

import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _Ctx:
    def register_hook(self, name, cb):
        pass

    def on_unload(self, cb):
        pass


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "slack_multiplayer_gateway_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(_Ctx())


# Registered before the gateway modules import, as in a real gateway start.
_load_plugin()

import gateway.run as gateway_run  # noqa: E402
from agent.prompt_builder import steer_user_row  # noqa: E402
from gateway.config import GatewayConfig, Platform  # noqa: E402
from gateway.platforms.event import MessageEvent  # noqa: E402
from gateway.response_filters import is_intentional_silence_response  # noqa: E402
from gateway.session import SessionEntry, SessionSource  # noqa: E402

GROUP_KEY = "agent:main:telegram:group:-1001:12345"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))


def _telegram_source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group",
                         user_id="12345")


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=GROUP_KEY, session_id="sess-silent", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="group")
    runner.session_store.load_transcript.return_value = []
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_a, **_kw: 100_000)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_expected", [None, True], ids=["adapter-unknown", "addressed"])
async def test_silence_marker_on_a_message_that_wanted_a_reply_stays_silent(
        monkeypatch, tmp_path, reply_expected):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "NO_REPLY",
        "messages": [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "NO_REPLY"}],
        "tools": [], "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1, "failed": False,
    })
    event = MessageEvent(text="hey", source=_telegram_source(), message_id="msg-1",
                         reply_expected=reply_expected)

    response = await runner._handle_message_with_agent(event, _telegram_source(), GROUP_KEY, 1)

    assert response == ""


def test_prose_replies_are_still_delivered():
    assert not is_intentional_silence_response("Use NO_REPLY when nothing is needed.")


SPEAKER = SessionSource(platform=Platform.SLACK, chat_id="C0000000CHN", chat_type="group",
                        user_id="U0000000ALX", user_name="Alex Example", scope_id="T0000000TEM")
TEXT = "one more thing"


class _Receiver:
    payload = None

    def steer(self, payload):
        self.payload = payload
        return True


async def _steer(runner, route):
    event = MessageEvent(text=f"/steer {TEXT}" if route == "explicit" else TEXT, source=SPEAKER,
                         message_id="1700000400.000100")
    receiver = _Receiver()
    runner._session_state("key").turn.agent = receiver
    if route == "explicit":
        await runner._busy_steer_command(event, "key", SPEAKER)
    elif route == "priority":
        runner._hm_busy_steer(event, receiver, "key")
    else:
        await runner._resolve_busy_steer_or_redirect(event, "key", "steer", receiver)
    return steer_user_row(receiver.payload)["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["explicit", "priority", "normal"])
async def test_steer_in_a_shared_chat_names_its_sender_like_a_normal_turn(route):
    shared = gateway_run.GatewayRunner(GatewayConfig(group_sessions_per_user=False))
    normal_turn = await shared._prepare_inbound_message_text(
        event=MessageEvent(text=TEXT, source=SPEAKER), source=SPEAKER, history=[])
    assert normal_turn == f"[Alex Example | Slack user <@U0000000ALX>] {TEXT}"

    lines = (await _steer(shared, route)).splitlines()

    assert normal_turn in lines
    assert lines[lines.index(normal_turn) + 1] == "[/OUT-OF-BAND USER MESSAGE]"
    assert any(line.startswith("Shared chat: this message is from the participant named")
               for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["explicit", "priority", "normal"])
async def test_steer_in_a_per_user_session_is_unchanged(route):
    per_user = gateway_run.GatewayRunner(GatewayConfig(group_sessions_per_user=True))

    row = await _steer(per_user, route)

    assert "Alex Example" not in row and "Shared chat:" not in row
    assert f"\n\n{TEXT}\n[/OUT-OF-BAND USER MESSAGE]" in row
