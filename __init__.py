"""Hermes tweaks for a bot in a busy multi-bot Slack channel.

1. Never post the silence-marker warning.

   When the model answers a bare NO_REPLY to a message the gateway thinks needed a reply, the
   gateway posts a visible "returned only a silence marker" warning instead. A deliberate
   silence is a choice, so this plugin makes every bare silence marker stay silent, on every
   platform, DMs and @mentions included.

2. Keep who a mention targets.

   Core Slack renders ``<@UID>`` as ``@DisplayName`` and deletes our own mention. In a
   channel full of bots that loses the target: the other Hermes bot's name is literally
   "bot", so "@bot ..." read like a mention of us. Inbound text now reads
   ``@relay (U0000000RLY)`` for other people and ``@helper bot (you)`` for our own bot id.
   Names come from the adapter's cached users.info lookup. Routing still runs on the raw
   Slack text, so who wakes the bot is unchanged.

3. Ignore channel messages addressed to someone else.

   Another bot in the workspace is literally named "Hermes", so "@Hermes ..." read like a
   message for us and we answered turns meant for it. Before the adapter sees a channel
   message, drop it when it @mentions others but not us, or when it's an untagged reply in a
   thread another bot started and we never posted in. DMs, our own threads and messages that
   tag us are unchanged.

4. Wake on replies in threads we posted in.

   Core wakes on an untagged thread reply only when the bot started the thread, was tagged
   in it, or already has a session there. A note posted with a raw chat.postMessage under
   someone else's root (an alert integration's message) doesn't count, so a human follow-up
   there never reached us. Now any message of ours in the thread counts. It reads the thread
   from Slack (cache first), so it also holds after a restart.

5. Name the sender on mid-turn messages in shared chats.

   In a shared multi-user session a normal turn starts with ``[Name | Slack user <@U...>]``.
   A message that lands mid-turn is steered into the running turn instead, and the steer
   marker says it is "from the user", so it read as coming from whoever started the turn.
   Steered text now carries the same sender prefix, plus a line saying who it is from.

All of this patches private Hermes internals. When upstream renames one, the matching patch
logs an error and that piece falls back to stock behavior. The gateway and the Slack adapter
are often imported after this plugin registers, so patches apply to already-imported modules
right away and to later ones from a meta-path finder right after the module executes.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import inspect
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

_ADAPTER_SUFFIX = os.path.join("plugins", "platforms", "slack", "adapter.py")
_PATCHED_FLAG = "_sme_mention_targets_patched"
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


# ----- mention targets -------------------------------------------------------


def _self_uids(adapter, chat_id: str, team_id: str) -> set:
    team_id = str(team_id or getattr(adapter, "_channel_team", {}).get(chat_id, ""))
    uids = {getattr(adapter, "_bot_user_id", None)}
    uids.add((getattr(adapter, "_team_bot_user_ids", None) or {}).get(team_id))
    return {u for u in uids if u}


async def _humanize_user_mentions(self, text: str, chat_id: str = "", team_id: str = "") -> str:
    """``<@UID>`` → ``@Name (UID)``; our own bot id → ``@Name (you)``."""
    if not text or "<@" not in text:
        return text
    mine = _self_uids(self, chat_id, team_id)
    labels: dict[str, str] = {}
    for uid in dict.fromkeys(_MENTION_RE.findall(text)):
        name = await self._resolve_user_name(uid, chat_id=chat_id, team_id=team_id)
        display = (name or "").strip() or uid
        if uid in mine:
            labels[uid] = f"@{display} (you)"
        elif display == uid:
            labels[uid] = f"@{uid}"
        else:
            labels[uid] = f"@{display} ({uid})"
    # Function replacement: user-set names are inserted verbatim, never parsed as templates.
    return _MENTION_RE.sub(lambda m: labels.get(m.group(1), m.group(0)), text)


def _make_apply_bot_mention(original):
    def _apply_bot_mention(self, text, original_text, command_probe_text, is_command_text,
                           bot_uid, thread_ts, team_id):
        result = original(self, text, original_text, command_probe_text, is_command_text,
                          bot_uid, thread_ts, team_id)
        new_text, new_original, new_probe, new_is_command = result
        if not new_text:
            # A bare ping stays empty as before. Commands are unaffected either way: the event
            # text for a command is the canonical command_probe_text.
            return result
        # Keep our mention in place; _humanize_user_mentions marks it "(you)".
        return text.strip(), new_original, new_probe, new_is_command
    _apply_bot_mention.__wrapped__ = original
    return _apply_bot_mention


_IDENTITY_NOTE = (
    ' Mentions in message text are shown as "@Name (USERID)"; a mention of you is shown as '
    '"@Name (you)". When a message @mentions someone else and not you, it is addressed to '
    "them, even if their name looks generic (for example another bot named \"bot\")."
    ' Other bots here may be named "Hermes"; that name never refers to you, so a message '
    'addressed to Hermes is not for you.')


# ----- messages addressed to someone else -------------------------------------


async def _addressed_elsewhere(adapter, event: dict) -> bool:
    """True for a channel message meant for someone else: it @mentions others but not us, or
    it's an untagged reply in a thread another bot started."""
    if not isinstance(event, dict) or event.get("channel_type") == "im":
        return False
    if event.get("subtype") not in (None, "", "thread_broadcast", "file_share"):
        return False
    chat_id = str(event.get("channel") or "")
    team_id = str(event.get("team") or event.get("team_id") or "")
    mine = _self_uids(adapter, chat_id, team_id)
    if not mine:
        return False
    mentioned = set(_MENTION_RE.findall(str(event.get("text") or "")))
    if mentioned & mine:
        return False
    if mentioned:
        return True
    ts, thread_ts = event.get("ts"), event.get("thread_ts")
    parent = event.get("parent_user_id")
    if not thread_ts or thread_ts == ts or not parent or parent in mine:
        return False
    if not await adapter._resolve_user_is_bot(parent, chat_id=chat_id, team_id=team_id):
        return False
    return not await _bot_replied_in_thread(adapter, chat_id, thread_ts, team_id)


def _make_handle_slack_message(original):
    async def _handle_slack_message(self, event, payload=None):
        if await _addressed_elsewhere(self, event):
            return None
        return await original(self, event, payload)
    _handle_slack_message.__wrapped__ = original
    return _handle_slack_message


# ----- wake on threads we posted in --------------------------------------------


async def _bot_replied_in_thread(adapter, channel_id: str, thread_ts: str, team_id: str = "") -> bool:
    """True when any message in the thread was posted by this bot. Reads conversations.replies
    through the adapter's thread cache, fetching once on a miss."""
    if not thread_ts:
        return False
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id) or ""
    if not bot_uid:
        return False
    for attempt in range(2):
        for key, entry in adapter._thread_context_cache.items():
            if key.startswith(f"{channel_id}:{thread_ts}:"):
                return any((m or {}).get("user") == bot_uid for m in (entry.messages or []))
        if attempt == 0:
            await adapter._fetch_thread_context(
                channel_id=channel_id, thread_ts=thread_ts, current_ts="", team_id=team_id)
    return False


def _make_should_wake(original):
    signature = inspect.signature(original)

    async def _should_wake_on_unmentioned_message(self, *args, **kwargs):
        if await original(self, *args, **kwargs):
            return True
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        a = bound.arguments
        return bool(a["is_thread_reply"] and await _bot_replied_in_thread(
            self, a["channel_id"], a["event_thread_ts"], a.get("team_id") or ""))
    _should_wake_on_unmentioned_message.__wrapped__ = original
    return _should_wake_on_unmentioned_message


def _make_build_identity_prompt(original):
    def _build_identity_prompt(self, team_id: str = "") -> str:
        base = original(self, team_id)
        return base + _IDENTITY_NOTE if base else base
    _build_identity_prompt.__wrapped__ = original
    return _build_identity_prompt


# ----- silence ----------------------------------------------------------------


def _silence_allowed(display_kind=None, reply_expected=None) -> bool:
    """A bare silence marker always stays silent."""
    return True


# ----- steer sender ---------------------------------------------------------------

_SHARED_STEER_NOTE = (
    "Shared chat: this message is from the participant named in its bracketed prefix, not "
    "necessarily the person whose message started this turn. Attribute it to that sender.\n")


def _shared_sender_label(runner, source):
    """The sender label a normal turn gets in a shared multi-user session, else None."""
    from gateway.config import Platform
    from gateway.session import is_shared_multi_user_session, neutralize_untrusted_inline_text

    shared = is_shared_multi_user_session(
        source, group_sessions_per_user=getattr(runner.config, "group_sessions_per_user", True),
        thread_sessions_per_user=getattr(runner.config, "thread_sessions_per_user", False))
    if not (shared and source.user_name):
        return None
    label = neutralize_untrusted_inline_text(source.user_name)
    if source.platform == Platform.SLACK and source.user_id:
        label = f"{label} | Slack user <@{source.user_id}>"
    return label


def _make_steer_text_with_origin(original):
    def _steer_text_with_origin(self, text, event):
        if text.strip() and event is not None:
            sender = _shared_sender_label(self, event.source)
            if sender:
                text = f"{_SHARED_STEER_NOTE}[{sender}] {text}"
        return original(self, text, event)
    _steer_text_with_origin.__wrapped__ = original
    return _steer_text_with_origin


# ----- applying patches -------------------------------------------------------------


def _wrap_method(cls, name, make):
    original = getattr(cls, name, None)
    if original is None:
        logger.error("slack-multiplayer-enhancements: %s.%s is gone; that patch is off",
                     cls.__name__, name)
        return
    setattr(cls, name, make(original))


def _patch_adapter_class(cls) -> bool:
    if cls is None or getattr(cls, _PATCHED_FLAG, False):
        return False
    cls._humanize_user_mentions = _humanize_user_mentions
    _wrap_method(cls, "_apply_bot_mention", _make_apply_bot_mention)
    _wrap_method(cls, "_build_identity_prompt", _make_build_identity_prompt)
    _wrap_method(cls, "_handle_slack_message", _make_handle_slack_message)
    _wrap_method(cls, "_should_wake_on_unmentioned_message", _make_should_wake)
    setattr(cls, _PATCHED_FLAG, True)
    return True


def _patch_response_filters(module) -> None:
    original = getattr(module, "silence_allowed", None)
    if original is None:
        logger.error("slack-multiplayer-enhancements: silence_allowed is gone; the silence patch is off")
        return
    if original is _silence_allowed:
        return
    # Modules that imported the function by name before this ran keep their own reference.
    for other in list(sys.modules.values()):
        if (getattr(other, "__name__", "") or "").startswith("gateway.") and \
                getattr(other, "silence_allowed", None) is original:
            other.silence_allowed = _silence_allowed


def _patch_run_busy(module) -> None:
    cls = getattr(module, "GatewayBusySessionMixin", None)
    if cls is None:
        logger.error("slack-multiplayer-enhancements: GatewayBusySessionMixin is gone; the steer patch is off")
        return
    if getattr(cls, _PATCHED_FLAG, False):
        return
    _wrap_method(cls, "_steer_text_with_origin", _make_steer_text_with_origin)
    setattr(cls, _PATCHED_FLAG, True)


def _is_slack_adapter_file(path) -> bool:
    return bool(path) and os.path.normpath(str(path)).endswith(_ADAPTER_SUFFIX)


_GATEWAY_PATCHES = {
    "gateway.response_filters": _patch_response_filters,
    "gateway.run_busy": _patch_run_busy,
}


def _patch_module(module) -> None:
    name = getattr(module, "__name__", "")
    if name in _GATEWAY_PATCHES:
        _GATEWAY_PATCHES[name](module)
    elif _is_slack_adapter_file(getattr(module, "__file__", None)):
        _patch_adapter_class(getattr(module, "SlackAdapter", None))


class _SlackAdapterPatchFinder(importlib.abc.MetaPathFinder):
    """Patch a target module right after it executes (the Slack adapter under whatever name it
    imports as, plus the gateway modules above)."""

    def find_spec(self, fullname, path, target=None):
        if fullname not in _GATEWAY_PATCHES and not fullname.endswith(".adapter"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return None
        if fullname not in _GATEWAY_PATCHES and not _is_slack_adapter_file(spec.origin):
            return None
        exec_module = spec.loader.exec_module

        def _exec_and_patch(module):
            exec_module(module)
            _patch_module(module)

        spec.loader.exec_module = _exec_and_patch
        return spec


_FINDER = _SlackAdapterPatchFinder()


def _is_patch_finder(finder) -> bool:
    # By name: a reloaded copy of this plugin defines a new class, and must still see the old finder.
    return type(finder).__name__ == _SlackAdapterPatchFinder.__name__


def install_mention_targets() -> None:
    for module in list(sys.modules.values()):
        if module is not None:
            _patch_module(module)
    if not any(_is_patch_finder(f) for f in sys.meta_path):
        sys.meta_path.insert(0, _FINDER)


def _uninstall_finder() -> None:
    sys.meta_path[:] = [f for f in sys.meta_path if not _is_patch_finder(f)]


def register(ctx):
    install_mention_targets()
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(_uninstall_finder)
