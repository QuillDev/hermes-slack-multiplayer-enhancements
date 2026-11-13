"""Inbound Slack text keeps who each @mention targets.

Run from the hermes-agent checkout so the real Slack adapter imports:
    cd ~/.hermes/hermes-agent && scripts/run-in-hermes-env python -m pytest -p no:cacheprovider \
        ~/.hermes/plugins/slack-multiplayer-enhancements/tests -q
"""

import asyncio
import time
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]

SELF = "U0000000SLF"      # our bot, "helper bot"
OTHER_HERMES = "U0000000BOT"  # the other Hermes bot, real name literally "bot"
RELAY = "U0000000RLY"
HUMAN = "U0000000ALX"

USERS = {
    SELF: {"is_bot": True, "name": "helper", "real_name": "helper bot",
           "profile": {"display_name": "", "real_name": "helper bot"}},
    OTHER_HERMES: {"is_bot": True, "name": "hermes", "real_name": "bot",
                   "profile": {"display_name": "", "real_name": "bot"}},
    RELAY: {"is_bot": True, "name": "relaybot", "real_name": "relay",
           "profile": {"display_name": "", "real_name": "relay"}},
    HUMAN: {"is_bot": False, "name": "alex", "real_name": "Alex Example",
            "profile": {"display_name": "Alex Example"}},
}


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt, slack_sdk = MagicMock(), MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt), ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler",
         slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk), ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())


class _Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def on_unload(self, cb):
        pass


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "slack_multiplayer_plugin_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(_Ctx())
    return module


_ensure_slack_mock()
# The plugin registers BEFORE the adapter module is imported, as in the gateway (the Slack
# adapter is a deferred platform plugin), so this exercises the lazy override.
_load_plugin()
import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True
from gateway.config import PlatformConfig  # noqa: E402


class FakeSlackUsers:
    """Stateful stand-in for Slack's users.info: answers from a directory, logs lookups."""

    def __init__(self, users):
        self.users = users
        self.lookups = []

    async def users_info(self, user):
        self.lookups.append(user)
        if user not in self.users:
            return {"ok": False, "error": "user_not_found"}
        return {"ok": True, "user": self.users[user]}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache")
    monkeypatch.setattr("gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache")


def _adapter(directory):
    a = _slack_mod.SlackAdapter(PlatformConfig(
        enabled=True, token="***", extra={"require_mention": False}))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._app.client.users_info = directory.users_info
    a._bot_user_id = SELF
    a._bot_display_name = "helper bot"
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _deliver(adapter, text, ts, user=HUMAN):
    event = {"text": text, "user": user, "channel": "C0000000CHN", "channel_type": "channel",
             "ts": ts}
    asyncio.run(adapter._handle_slack_message(event))
    assert adapter.handle_message.await_count >= 1, "message was not dispatched"
    return adapter.handle_message.await_args.args[0]


def test_other_bot_mention_names_the_target_with_its_user_id():
    adapter = _adapter(FakeSlackUsers(USERS))
    event = _deliver(adapter, f"<@{RELAY}> is this thing on <@{SELF}>", "1700000000.000100")
    assert event.text == f"@relay ({RELAY}) is this thing on @helper bot (you)"


def test_bot_literally_named_bot_is_not_mistaken_for_us():
    adapter = _adapter(FakeSlackUsers(USERS))
    event = _deliver(
        adapter, f"<@{OTHER_HERMES}|bot> please evaluate everyone in this channel, cc <@{SELF}>",
        "1700000000.000200")
    assert event.text == (
        f"@bot ({OTHER_HERMES}) please evaluate everyone in this channel, cc @helper bot (you)")


def test_our_own_mention_is_kept_and_marked_as_us_next_to_others():
    adapter = _adapter(FakeSlackUsers(USERS))
    event = _deliver(
        adapter, f"<@{SELF}> vs <@{OTHER_HERMES}> who is better", "1700000000.000300")
    assert event.text == f"@helper bot (you) vs @bot ({OTHER_HERMES}) who is better"


def test_unknown_user_falls_back_to_the_bare_id():
    adapter = _adapter(FakeSlackUsers(USERS))
    event = _deliver(adapter, f"<@U0NOBODY00> hi <@{SELF}>", "1700000000.000400")
    assert event.text == "@U0NOBODY00 hi @helper bot (you)"


def test_command_behind_our_mention_still_reaches_the_command_parser():
    adapter = _adapter(FakeSlackUsers(USERS))
    event = _deliver(adapter, f"<@{SELF}> /status", "1700000000.000500")
    assert event.text == "/status"


def test_names_are_looked_up_once_per_user_across_messages():
    directory = FakeSlackUsers(USERS)
    adapter = _adapter(directory)
    _deliver(adapter, f"<@{RELAY}> one <@{SELF}>", "1700000000.000600")
    event = _deliver(adapter, f"<@{RELAY}> two <@{RELAY}> <@{SELF}>", "1700000000.000700")
    assert event.text == f"@relay ({RELAY}) two @relay ({RELAY}) @helper bot (you)"
    assert directory.lookups.count(RELAY) == 1


NAMED_HERMES = "U0000000HRM"  # another bot whose Slack name is literally "Hermes"
USERS[NAMED_HERMES] = {"is_bot": True, "name": "otherbot", "real_name": "Hermes",
                    "profile": {"display_name": "Hermes", "real_name": "Hermes"}}


def _deliver_raw(adapter, **fields):
    event = {"user": HUMAN, "channel": "C0000000CHN", "channel_type": "channel", **fields}
    asyncio.run(adapter._handle_slack_message(event))
    return adapter.handle_message.await_count


def test_message_tagging_only_another_bot_named_hermes_never_reaches_us():
    adapter = _adapter(FakeSlackUsers(USERS))
    for i, text in enumerate([
        f"<@{NAMED_HERMES}> welcome to the channel",
        f"no <@{NAMED_HERMES}> you, not the other one",
        f"<@{NAMED_HERMES}> !restart",
    ]):
        assert _deliver_raw(adapter, text=text, ts=f"1700000100.00010{i}") == 0, text


def test_untagged_reply_in_another_bots_thread_never_reaches_us():
    adapter = _adapter(FakeSlackUsers(USERS))
    root = "1700000000.000001"
    count = _deliver_raw(adapter, text="you can see every message here, it's a setting",
                         ts="1700000100.000100", thread_ts=root, parent_user_id=NAMED_HERMES)
    assert count == 0


def test_messages_meant_for_us_still_arrive():
    adapter = _adapter(FakeSlackUsers(USERS))
    root = "1700000000.000001"
    _deliver_raw(adapter, text=f"<@{SELF}> and <@{NAMED_HERMES}> who is better", ts="1700000200.000100")
    _deliver_raw(adapter, text=f"<@{SELF}> what do you think", ts="1700000200.000200",
                 thread_ts=root, parent_user_id=NAMED_HERMES)
    _deliver_raw(adapter, text="untagged room chatter", ts="1700000200.000300")
    _deliver_raw(adapter, text="replying to a person", ts="1700000200.000400",
                 thread_ts="1700000200.000001", parent_user_id=HUMAN)
    _deliver_raw(adapter, text="following up on your note", ts="1700000200.000500",
                 thread_ts="1700000200.000002", parent_user_id=SELF)
    texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
    assert texts == [
        f"@helper bot (you) and @Hermes ({NAMED_HERMES}) who is better",
        "@helper bot (you) what do you think",
        "untagged room chatter",
        "replying to a person",
        "following up on your note",
    ]


def test_identity_prompt_says_hermes_in_slack_is_not_us():
    adapter = _adapter(FakeSlackUsers(USERS))
    assert 'that name never refers to you' in adapter._build_identity_prompt()


def test_identity_prompt_explains_the_mention_format():
    adapter = _adapter(FakeSlackUsers(USERS))
    prompt = adapter._build_identity_prompt()
    assert '"@helper bot"' in prompt
    assert "(you)" in prompt and "(USERID)" in prompt


# ---------------------------------------------------------------------------
# Untagged replies in a thread we posted in (raw chat.postMessage under someone else's root)
# ---------------------------------------------------------------------------

ALERTS = "U0000000ALR"  # an alert integration that posts thread roots
USERS[ALERTS] = {"is_bot": True, "name": "alerts", "real_name": "alerts",
                 "profile": {"display_name": "alerts", "real_name": "alerts"}}


def _mention_gated_adapter(thread_authors, root):
    adapter = _adapter(FakeSlackUsers(USERS))
    adapter.config.extra["require_mention"] = True
    adapter._thread_context_cache = {
        f"C0000000CHN:{root}:": _slack_mod._ThreadContextCache(
            content="ctx", fetched_at=time.monotonic(), message_count=len(thread_authors),
            parent_text="alert fired", parent_user_id=thread_authors[0],
            messages=[{"user": u, "ts": f"1700000300.00000{i}"} for i, u in enumerate(thread_authors)]),
    }
    return adapter


def test_untagged_reply_reaches_us_in_a_thread_we_posted_in():
    root = "1700000300.000000"
    adapter = _mention_gated_adapter([ALERTS, SELF], root)
    count = _deliver_raw(adapter, text="mark it as resolved", ts="1700000300.000900",
                         thread_ts=root, parent_user_id=ALERTS)
    assert count == 1
    assert adapter.handle_message.await_args.args[0].text == "mark it as resolved"


def test_untagged_reply_in_a_thread_we_never_posted_in_stays_ignored():
    root = "1700000300.000000"
    for authors in ([ALERTS, HUMAN], [HUMAN, RELAY]):
        adapter = _mention_gated_adapter(authors, root)
        count = _deliver_raw(adapter, text="mark it as resolved", ts="1700000300.000900",
                             thread_ts=root, parent_user_id=authors[0])
        assert count == 0, authors


# ---------------------------------------------------------------------------
# Thread history uses the same mention format as live messages
# ---------------------------------------------------------------------------


class FakeSlackThread:
    """conversations.replies answering from one fixed thread."""

    def __init__(self, messages):
        self.messages = messages

    async def conversations_replies(self, channel, ts, limit=None, inclusive=True, **_kw):
        return {"ok": True, "messages": [m for m in self.messages
                                         if m.get("thread_ts", m["ts"]) == ts][:limit]}


def test_thread_history_shows_mentions_like_live_messages():
    root = "1700000400.000000"
    thread = FakeSlackThread([
        {"ts": root, "user": HUMAN, "text": f"<@{RELAY}> can you check the deploy", "client_msg_id": "a"},
        {"ts": "1700000400.000100", "thread_ts": root, "user": RELAY, "bot_id": "B0000000RLY",
         "text": f"<@{HUMAN}> on it, cc <@{OTHER_HERMES}|bot>"},
        {"ts": "1700000400.000200", "thread_ts": root, "user": HUMAN, "client_msg_id": "b",
         "text": f"<@{SELF}> do you agree with <@{RELAY}>?"},
        {"ts": "1700000400.000300", "thread_ts": root, "user": SELF, "bot_id": "B0000000SLF",
         "text": f"<@{HUMAN}> mostly, yes"},
        {"ts": "1700000400.000900", "thread_ts": root, "user": HUMAN, "client_msg_id": "c",
         "text": f"<@{SELF}> and now?"},
    ])
    adapter = _adapter(FakeSlackUsers(USERS))
    adapter._app.client.conversations_replies = thread.conversations_replies

    event = asyncio.run(_deliver_thread_reply(adapter, thread.messages[-1]))

    history = event.channel_context
    assert "<@" not in history
    assert f"Alex Example: @relay ({RELAY}) can you check the deploy" in history
    assert f"relay: @Alex Example ({HUMAN}) on it, cc @bot ({OTHER_HERMES})" in history
    assert f"Alex Example: @helper bot (you) do you agree with @relay ({RELAY})?" in history
    assert f"[assistant] @Alex Example ({HUMAN}) mostly, yes" in history
    assert event.text == "@helper bot (you) and now?"


async def _deliver_thread_reply(adapter, msg):
    event = {**msg, "channel": "C0000000CHN", "channel_type": "channel"}
    await adapter._handle_slack_message(event)
    assert adapter.handle_message.await_count == 1, "thread reply was not dispatched"
    return adapter.handle_message.await_args.args[0]


# ---------------------------------------------------------------------------
# Untagged follow-ups and edited mentions in an alert's thread
# ---------------------------------------------------------------------------

ALERT_ROOT = "1700000500.000000"


class LiveSlackThread(FakeSlackThread):
    """conversations.replies over a thread that keeps growing while the test runs."""

    def post(self, user, text, ts, **extra):
        msg = {"ts": ts, "thread_ts": ALERT_ROOT, "user": user, "text": text, **extra}
        self.messages.append(msg)
        return msg


def _alert_thread_adapter():
    thread = LiveSlackThread([
        {"ts": ALERT_ROOT, "user": ALERTS, "bot_id": "B0000000ALR", "text": "deliver failed"}])
    adapter = _adapter(FakeSlackUsers(USERS))
    adapter.config.extra["require_mention"] = True
    adapter._app.client.conversations_replies = thread.conversations_replies

    async def chat_postMessage(channel, text, thread_ts=None, **_kw):
        ts = f"1700000500.{len(thread.messages):06d}9"
        thread.post(SELF, text, ts, bot_id="B0000000SLF")
        return {"ok": True, "ts": ts, "channel": channel}

    adapter._app.client.chat_postMessage = chat_postMessage
    return adapter, thread


def _human_reply(thread, text, ts):
    msg = thread.post(HUMAN, text, ts, client_msg_id=f"m-{ts}", parent_user_id=ALERTS)
    return {**msg, "type": "message", "channel": "C0000000CHN", "channel_type": "channel"}


def test_untagged_follow_ups_reach_us_once_we_were_tagged_in_the_alert_thread():
    adapter, thread = _alert_thread_adapter()

    asyncio.run(adapter._handle_slack_message(
        _human_reply(thread, f"<@{SELF}> did we handle this", "1700000500.000100")))
    # Sent while our answer is still being written, then again after it landed.
    asyncio.run(adapter._handle_slack_message(
        _human_reply(thread, "and check the retries too", "1700000500.000200")))
    asyncio.run(adapter.send("C0000000CHN", "not yet", reply_to="1700000500.000100",
                             metadata={"thread_id": ALERT_ROOT}))
    asyncio.run(adapter._handle_slack_message(
        _human_reply(thread, "can you open a thread for it?", "1700000500.000300")))

    texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
    assert texts == ["@helper bot (you) did we handle this", "and check the retries too",
                     "can you open a thread for it?"]


def test_untagged_follow_up_reaches_us_after_our_reply_outdated_the_saved_thread_copy():
    adapter, thread = _alert_thread_adapter()
    asyncio.run(adapter._fetch_thread_context(
        channel_id="C0000000CHN", thread_ts=ALERT_ROOT, current_ts=""))
    for entry in adapter._thread_context_cache.values():
        entry.fetched_at -= 30  # saved half a minute ago, before we posted
    thread.post(SELF, "found the cause", "1700000500.000200", bot_id="B0000000SLF")

    asyncio.run(adapter._handle_slack_message(
        _human_reply(thread, "nice, fix it", "1700000500.000300")))

    assert [c.args[0].text for c in adapter.handle_message.await_args_list] == ["nice, fix it"]


def test_editing_a_tag_into_an_ignored_reply_starts_exactly_one_turn():
    orig_ts, edit_ts = "1700000500.000400", "1700000515.000000"
    for order in ("mention first", "change first"):
        adapter, thread = _alert_thread_adapter()
        original = _human_reply(thread, "please read the above", orig_ts)
        asyncio.run(adapter._handle_slack_message(original))
        assert adapter.handle_message.await_count == 0

        edited = {**original, "text": f"<@{SELF}>  please read the above",
                  "edited": {"user": HUMAN, "ts": edit_ts}}
        app_mention = {**edited, "type": "app_mention", "event_ts": edit_ts}
        app_mention.pop("channel_type")
        changed = {"type": "message", "subtype": "message_changed", "hidden": True,
                   "channel": "C0000000CHN", "channel_type": "channel", "ts": edit_ts,
                   "event_ts": edit_ts, "message": {k: v for k, v in edited.items()
                                                     if k not in ("channel", "channel_type")},
                   "previous_message": {"ts": orig_ts, "user": HUMAN, "text": original["text"]}}
        events = [app_mention, changed] if order == "mention first" else [changed, app_mention]
        for event in events:
            asyncio.run(adapter._handle_slack_message(event))

        texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
        assert texts == ["@helper bot (you)  please read the above"], order
