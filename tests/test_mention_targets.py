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
