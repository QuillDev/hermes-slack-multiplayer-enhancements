"""Slack tweaks for a Hermes bot in a busy multi-bot channel.

1. Stay quiet on unaddressed Slack channel chatter.

   The Slack adapter marks a channel message it can't classify (not an @mention, not
   peer-addressed) as reply_expected=None, and the gateway then turns a bare NO_REPLY into
   a visible "returned only a silence marker" warning. Untagged room chatter ("hype",
   "oh wow") is not a message that needs a reply, so mark it reply_expected=False.
   DMs and @mentions are already True and keep the warning.

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
   thread another bot started. DMs, our own threads and messages that tag us are unchanged.

   The adapter is a deferred bundled platform plugin, usually imported after this plugin
   registers, so the override is applied to whichever SlackAdapter class gets loaded:
   already-imported ones right away, later ones by a meta-path finder that patches the
   module right after it executes.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import re
import sys

_ADAPTER_SUFFIX = os.path.join("plugins", "platforms", "slack", "adapter.py")
_PATCHED_FLAG = "_sme_mention_targets_patched"
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


def _quiet_unaddressed(event=None, **_kwargs):
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if (platform == "slack" and getattr(source, "chat_type", "dm") != "dm"
            and getattr(event, "reply_expected", True) is None):
        event.reply_expected = False
    return None


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
    return bool(await adapter._resolve_user_is_bot(parent, chat_id=chat_id, team_id=team_id))


def _make_handle_slack_message(original):
    async def _handle_slack_message(self, event, payload=None):
        if await _addressed_elsewhere(self, event):
            return None
        return await original(self, event, payload)
    _handle_slack_message.__wrapped__ = original
    return _handle_slack_message


def _make_build_identity_prompt(original):
    def _build_identity_prompt(self, team_id: str = "") -> str:
        base = original(self, team_id)
        return base + _IDENTITY_NOTE if base else base
    _build_identity_prompt.__wrapped__ = original
    return _build_identity_prompt


def _patch_adapter_class(cls) -> bool:
    if cls is None or getattr(cls, _PATCHED_FLAG, False):
        return False
    cls._humanize_user_mentions = _humanize_user_mentions
    cls._apply_bot_mention = _make_apply_bot_mention(cls._apply_bot_mention)
    cls._build_identity_prompt = _make_build_identity_prompt(cls._build_identity_prompt)
    cls._handle_slack_message = _make_handle_slack_message(cls._handle_slack_message)
    setattr(cls, _PATCHED_FLAG, True)
    return True


def _is_slack_adapter_file(path) -> bool:
    return bool(path) and os.path.normpath(str(path)).endswith(_ADAPTER_SUFFIX)


def _patch_module(module) -> None:
    if _is_slack_adapter_file(getattr(module, "__file__", None)):
        _patch_adapter_class(getattr(module, "SlackAdapter", None))


class _SlackAdapterPatchFinder(importlib.abc.MetaPathFinder):
    """Patch the Slack adapter module right after it executes, whatever name it imports under."""

    def find_spec(self, fullname, path, target=None):
        if not fullname.endswith(".adapter"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None or not _is_slack_adapter_file(spec.origin):
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
    ctx.register_hook("pre_gateway_dispatch", _quiet_unaddressed)
    install_mention_targets()
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(_uninstall_finder)
