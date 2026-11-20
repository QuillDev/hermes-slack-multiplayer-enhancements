"""Slack plan card: a tool-using turn shows a live plan in the message's thread.

Runs a real gateway turn (fake agent) through the real Slack adapter; only Slack's Web API is
fake, served over HTTP to a real ``AsyncWebClient``. Run from a hermes-agent checkout with its
dev environment (see README).
"""

import asyncio
import importlib
import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _Ctx:
    def register_hook(self, name, cb):
        pass

    def on_unload(self, cb):
        pass


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "slack_multiplayer_plan_card_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(_Ctx())


_load_plugin()

aiohttp_web = pytest.importorskip("aiohttp.web")
slack_web = pytest.importorskip("slack_sdk.web.async_client")

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402

CHANNEL = "C0000000001"
DM = "D0000000001"
MESSAGE_TS = "1700000000.000100"
CARD_TS = "1700000000.000200"
SECRET_ARG = "select * from vault where token = 'xoxb-NOT-A-REAL-TOKEN'"
SECRET_ERROR = "permission denied for relation vault_secrets"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))


class FakeSlack:
    """Slack's Web API over HTTP: keeps the messages a channel would show."""

    def __init__(self, fail=(), fail_once=()):
        self.fail = set(fail)
        self.fail_once = set(fail_once)
        self.calls = []
        self.call_times = []
        self.messages = {}  # ts -> {"thread_ts", "chunks", "streaming", "text"}
        self.status_shown = {}  # thread ts -> every status it showed ("" = cleared)
        self._seq = 0

    async def handle(self, request):
        method = request.match_info["method"]
        body = await request.json() if request.can_read_body else {}
        self.calls.append((method, body))
        self.call_times.append((method, time.monotonic()))
        if method in self.fail_once:
            self.fail_once.discard(method)
            return aiohttp_web.json_response({"ok": False, "error": "ratelimited"})
        if method in self.fail:
            return aiohttp_web.json_response({"ok": False, "error": "internal_error"})
        if method == "chat.startStream":
            self.messages[CARD_TS] = {"thread_ts": body.get("thread_ts"),
                                      "chunks": list(body.get("chunks") or []),
                                      "streaming": True}
            return aiohttp_web.json_response({"ok": True, "channel": CHANNEL, "ts": CARD_TS})
        if method in {"chat.appendStream", "chat.stopStream"}:
            msg = self.messages.get(body.get("ts"))
            if not msg or not msg["streaming"]:
                return aiohttp_web.json_response({"ok": False, "error": "message_not_in_streaming_state"})
            msg["chunks"].extend(body.get("chunks") or [])
            if body.get("markdown_text"):
                msg["text"] = msg.get("text", "") + body["markdown_text"]
            msg["streaming"] = method == "chat.appendStream"
            return aiohttp_web.json_response({"ok": True, "channel": CHANNEL, "ts": body["ts"]})
        if method == "chat.delete":
            existed = self.messages.pop(body.get("ts"), None)
            return aiohttp_web.json_response({"ok": bool(existed), "error": None if existed else "message_not_found"})
        if method == "chat.postMessage":
            self._seq += 1
            ts = f"1700000001.{self._seq:06d}"
            self.messages[ts] = {"thread_ts": body.get("thread_ts"), "text": body.get("text"), "streaming": False}
            if body.get("blocks"):
                self.messages[ts]["blocks"] = body["blocks"]
            return aiohttp_web.json_response({"ok": True, "channel": CHANNEL, "ts": ts})
        if method == "agents.sessions.setStatus" and body.get("status") not in {
                "processing", "active", "suspended", "closed"}:
            return aiohttp_web.json_response({"ok": False, "error": "invalid_arguments"})
        if method in {"assistant.threads.setStatus", "agents.sessions.setStatus"}:
            self.status_shown.setdefault(body.get("thread_ts"), []).append(body.get("status"))
            return aiohttp_web.json_response({"ok": True})
        if method == "chat.update":
            msg = self.messages.get(body.get("ts"))
            if not msg:
                return aiohttp_web.json_response({"ok": False, "error": "message_not_found"})
            msg["blocks"] = body.get("blocks")
            return aiohttp_web.json_response({"ok": True, "channel": CHANNEL, "ts": body["ts"]})
        return aiohttp_web.json_response({"ok": True})

    def card(self):
        return self.messages.get(CARD_TS)

    def task_states(self):
        """Each task's status sequence, by task id, as the card received them."""
        states = {}
        for chunk in (self.card() or {}).get("chunks", []):
            if chunk.get("type") == "task_update":
                seq = states.setdefault(chunk["id"], [])
                if not seq or seq[-1] != chunk["status"]:
                    seq.append(chunk["status"])
        return states

    def plan_frames(self):
        """Every plan block a flat card showed, in order: as posted, then each edit."""
        return [block for method, body in self.calls if method in {"chat.postMessage", "chat.update"}
                for block in body.get("blocks") or [] if block.get("type") == "plan"]

    def flat_task_states(self):
        states = {}
        for plan in self.plan_frames():
            for task in plan["tasks"]:
                seq = states.setdefault(task["task_id"], [])
                if not seq or seq[-1] != task["status"]:
                    seq.append(task["status"])
        return states

    def wire_text(self):
        return json.dumps(self.calls)


@pytest_asyncio.fixture
async def slack():
    fakes = []

    async def start(**kw):
        fake = FakeSlack(**kw)
        app = aiohttp_web.Application()
        app.router.add_post("/api/{method}", fake.handle)
        runner = aiohttp_web.AppRunner(app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        fake.client = slack_web.AsyncWebClient(token="xoxb-test", base_url=f"http://127.0.0.1:{port}/api/")
        fakes.append(runner)
        return fake

    yield start
    for runner in fakes:
        await runner.cleanup()


class ToolAgent:
    """Calls a query tool that succeeds and one that fails, then answers."""
    final = "here's the answer"

    def __init__(self, **kwargs):
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.tools = []

    def start(self, *args):
        if self.tool_start_callback:
            self.tool_start_callback(*args)

    def complete(self, *args):
        if self.tool_complete_callback:
            self.tool_complete_callback(*args)

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        self.start("call-1", "mcp__slack__conversations_history", {"channel_id": CHANNEL})
        time.sleep(0.1)
        self.complete("call-1", "mcp__slack__conversations_history", {"channel_id": CHANNEL}, '{"ok": true}')
        self.start("call-2", "mcp__autopat__query_snowflake", {"sql": SECRET_ARG})
        time.sleep(1.3)  # longer than the card's one-frame-a-second pace
        self.complete("call-2", "mcp__autopat__query_snowflake", {"sql": SECRET_ARG},
                      json.dumps({"error": SECRET_ERROR}))
        time.sleep(0.1)
        return {"final_response": self.final, "messages": [], "api_calls": 1}


class SilentToolAgent(ToolAgent):
    final = "NO_REPLY"


class NoToolAgent(ToolAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        time.sleep(0.2)
        return {"final_response": "just chatting", "messages": [], "api_calls": 1}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False,
                                    stt_enabled=False)
    return runner


ROOT_TS = "1700000000.000050"  # an existing thread the user's message sits in


async def _turn(monkeypatch, tmp_path, fake, agent_cls, *, cards=True, where="thread", addressed=True):
    """One addressed turn, driven in the order the gateway's message handler runs it: the eyes
    hook, the agent run, the final send, then the completion hook. ``where``: "channel" or "dm"
    for a top-level message, "thread" for a channel message inside an existing thread."""
    from gateway.platforms.base import _thread_metadata_for_source
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.response_filters import is_intentional_silence_agent_result

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    extra = {"reply_in_thread": False, "dm_top_level_threads_as_sessions": False}
    if cards:
        extra["native_task_cards"] = True
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test", extra=extra))
    adapter._app = SimpleNamespace(client=fake.client)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    chat = DM if where == "dm" else CHANNEL
    chat_type = "dm" if where == "dm" else "group"
    # Flat replies: a top-level message has no thread; a thread reply carries its root.
    thread = ROOT_TS if where == "thread" else None
    source = SessionSource(platform=Platform.SLACK, chat_id=chat, chat_type=chat_type, thread_id=thread,
                           user_id="U0000000001", scope_id="T0000000001")
    if addressed:
        adapter._track_reacting_message("T0000000001", MESSAGE_TS)  # the eyes go on
    event = MessageEvent(text="hello", source=source, message_id=MESSAGE_TS)
    await adapter.on_processing_start(event)
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source, session_id="sess-card",
        session_key=f"agent:main:slack:{chat_type}:{chat}:{thread}", event_message_id=MESSAGE_TS)
    if not is_intentional_silence_agent_result(result, result.get("final_response")):
        await adapter.send(chat, result["final_response"], reply_to=MESSAGE_TS,
                           metadata=_thread_metadata_for_source(source, MESSAGE_TS))
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    return result


def _left(fake):
    """What the chat shows afterwards, in order: a card's step states, or a reply's text."""
    shown = []
    for msg in fake.messages.values():
        plans = [b for b in msg.get("blocks") or [] if b.get("type") == "plan"]
        shown.append([(t["title"], t["status"]) for t in plans[0]["tasks"]] if plans else msg.get("text"))
    return shown


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["channel", "dm"])
async def test_top_level_turn_keeps_a_flat_card_above_its_one_answer(monkeypatch, tmp_path, slack, where):
    fake = await slack()

    result = await _turn(monkeypatch, tmp_path, fake, ToolAgent, where=where)

    plans = fake.plan_frames()
    assert {plan["title"] for plan in plans} == {"working on it"}
    assert [t["title"] for t in plans[0]["tasks"]] == ["thinking"]  # up before any tool ran
    # Shown running, then ended with its result (the flat pace may fold a short step's frames).
    states = fake.flat_task_states()
    assert states["call-1"] == ["in_progress", "complete"] and states["call-2"][-1] == "error"
    assert _left(fake) == [[("checking slack", "complete"), ("running a query", "error")], result["final_response"]]
    # Nothing opened a thread.
    assert not any(body.get("thread_ts") for _, body in fake.calls)
    assert "chat.startStream" not in [m for m, _ in fake.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["channel", "dm"])
@pytest.mark.parametrize("agent_cls", [NoToolAgent, SilentToolAgent], ids=["no-tools", "no-reply"])
async def test_top_level_chat_turn_keeps_its_card_and_a_silent_one_leaves_nothing(
        monkeypatch, tmp_path, slack, agent_cls, where):
    fake = await slack()

    result = await _turn(monkeypatch, tmp_path, fake, agent_cls, where=where)

    assert fake.plan_frames()  # the card was up while the turn ran
    expected = [] if agent_cls is SilentToolAgent else [[("thinking", "complete")], result["final_response"]]
    assert _left(fake) == expected


@pytest.mark.asyncio
async def test_unaddressed_channel_turn_shows_no_card(monkeypatch, tmp_path, slack):
    fake = await slack()

    await _turn(monkeypatch, tmp_path, fake, SilentToolAgent, where="channel", addressed=False)

    assert not fake.plan_frames() and fake.calls == []


@pytest.mark.asyncio
async def test_a_silent_turns_card_is_gone_even_when_slack_refuses_the_first_delete(monkeypatch, tmp_path, slack):
    fake = await slack(fail_once={"chat.delete"})

    await _turn(monkeypatch, tmp_path, fake, SilentToolAgent, where="channel")

    assert fake.plan_frames() and fake.messages == {}


@pytest.mark.asyncio
async def test_card_edits_across_busy_turns_stay_under_slacks_edit_rate(slack):
    from gateway.platforms.event import MessageEvent
    fake = await slack()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test", extra={
        "reply_in_thread": False, "native_task_cards": True}))
    adapter._app = SimpleNamespace(client=fake.client)
    anchors = [f"1700000000.00030{i}" for i in range(3)]  # three turns running at once in one channel
    for ts in anchors:
        adapter._track_reacting_message("T0000000001", ts)
        await adapter.on_processing_start(MessageEvent(text="hi", message_id=ts, source=SessionSource(
            platform=Platform.SLACK, chat_id=CHANNEL, chat_type="group", user_id="U0000000001",
            scope_id="T0000000001")))

    async def edit(ts):
        for status in ("in_progress", "complete"):
            await adapter.send_native_task_card_progress(
                CHANNEL, [{"id": "t", "title": "reading files", "status": status}],
                metadata={"sme_card_anchor_ts": ts})

    await asyncio.gather(*(edit(ts) for ts in anchors))

    times = [at for method, at in fake.call_times if method == "chat.update"]
    assert len(times) == 6
    assert min(b - a for a, b in zip(times, times[1:])) >= 1.1  # at most ~50 edits a minute


@pytest.mark.asyncio
async def test_thread_turn_shows_a_plan_in_its_thread_then_answers_there(monkeypatch, tmp_path, slack):
    fake = await slack()

    result = await _turn(monkeypatch, tmp_path, fake, ToolAgent)

    card = fake.card()
    assert card["thread_ts"] == ROOT_TS and card["streaming"] is False
    assert fake.task_states() == {"call-1": ["in_progress", "complete"],
                                  "call-2": ["in_progress", "error"]}
    titles = {c["title"] for c in card["chunks"] if c["type"] == "task_update"}
    assert titles == {"checking slack", "running a query"}
    assert {c["title"] for c in card["chunks"] if c["type"] == "plan_update"} == {"working on it"}
    assert not fake.plan_frames()  # no flat card on top of it
    answers = [m for ts, m in fake.messages.items() if ts != CARD_TS]
    assert answers == [{"thread_ts": ROOT_TS, "text": result["final_response"], "streaming": False}]


@pytest.mark.asyncio
async def test_thread_turns_show_the_responding_status_and_top_level_dms_do_not(slack):
    from gateway.platforms.base import _thread_metadata_for_source
    from gateway.platforms.helpers import typing_metadata_for_turn
    fake = await slack()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test", extra={
        "reply_in_thread": False, "dm_top_level_threads_as_sessions": False}))
    adapter._app = SimpleNamespace(client=fake.client)
    cases = {(CHANNEL, ROOT_TS): "group", (DM, ROOT_TS): "dm", (DM, None): "dm", (CHANNEL, None): "group"}

    for (chat, thread), chat_type in cases.items():
        source = SessionSource(platform=Platform.SLACK, chat_id=chat, chat_type=chat_type, thread_id=thread,
                               user_id="U0000000001", scope_id="T0000000001")
        # The turn's status metadata, as the gateway fixes it when the eyes go on.
        metadata = typing_metadata_for_turn(adapter, chat, MESSAGE_TS, _thread_metadata_for_source(source, MESSAGE_TS))
        await adapter.send_typing(chat, metadata=metadata)
        await adapter.stop_typing(chat, metadata=metadata)

    # Shown, then cleared, on the thread (channel and DM); never on a top-level message.
    assert fake.status_shown == {ROOT_TS: ["is thinking...", "", "is thinking...", ""]}
    assert fake.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["thread", "channel"])
async def test_failing_tool_puts_no_args_names_or_error_text_on_the_wire(monkeypatch, tmp_path, slack, where):
    fake = await slack()

    await _turn(monkeypatch, tmp_path, fake, ToolAgent, where=where)

    wire = fake.wire_text()
    states = fake.flat_task_states() if where == "channel" else fake.task_states()
    assert "error" in json.dumps(states)  # the failure did reach the card
    for leaked in (SECRET_ARG, "«redacted:xox…»", SECRET_ERROR, "vault",
                   "query_snowflake", "conversations_history"):
        assert leaked not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [NoToolAgent, SilentToolAgent], ids=["no-tools", "no-reply"])
async def test_no_tool_and_silent_thread_turns_leave_no_card(monkeypatch, tmp_path, slack, agent_cls):
    fake = await slack()

    result = await _turn(monkeypatch, tmp_path, fake, agent_cls)

    assert _left(fake) == ([] if agent_cls is SilentToolAgent else [result["final_response"]])


@pytest.mark.asyncio
async def test_streaming_error_falls_back_to_the_plain_reply(monkeypatch, tmp_path, slack):
    fake = await slack(fail={"chat.startStream"})

    result = await _turn(monkeypatch, tmp_path, fake, ToolAgent)

    assert _left(fake) == [result["final_response"]]  # no card and no text progress bubble


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["thread", "channel"])
async def test_flag_off_shows_no_card(monkeypatch, tmp_path, slack, where):
    fake = await slack()

    result = await _turn(monkeypatch, tmp_path, fake, ToolAgent, cards=False, where=where)

    assert _left(fake) == [result["final_response"]]
    assert not fake.plan_frames() and "chat.startStream" not in [m for m, _ in fake.calls]


@pytest.mark.asyncio
async def test_bursts_of_tool_events_are_coalesced(monkeypatch, tmp_path, slack):
    class BurstAgent(ToolAgent):
        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            for i in range(20):
                self.start(f"call-{i}", "web_search", {"query": str(i)})
                self.complete(f"call-{i}", "web_search", {"query": str(i)}, "{}")
            time.sleep(0.3)
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake = await slack()

    await _turn(monkeypatch, tmp_path, fake, BurstAgent)

    appends = [m for m, _ in fake.calls if m == "chat.appendStream"]
    assert len(appends) <= 3
    # Every task ends complete on the card, including ones that scrolled out of view.
    states = fake.task_states()
    assert len(states) == 20
    assert {seq[-1] for seq in states.values()} == {"complete"}
