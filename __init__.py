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
   tag us are unchanged. A thread we were tagged in earlier counts as ours, so a follow-up
   sent while our answer is still being written arrives. A dropped message still counts as
   seen (as core's own filter would record it), so an edit that adds our mention, which Slack
   sends twice (an app_mention and a message_changed), starts one turn instead of a second
   copy that fails admission.

4. Wake on replies in threads we posted in.

   Core wakes on an untagged thread reply only when the bot started the thread, was tagged
   in it, or already has a session there. A note posted with a raw chat.postMessage under
   someone else's root (an alert integration's message) doesn't count, so a human follow-up
   there never reached us. Now any message of ours in the thread counts. It reads the thread
   from Slack (cache first), so it also holds after a restart. A cached copy that says we
   never posted is re-read when it is more than a few seconds old, since it may predate our
   own reply.

5. Name the sender on mid-turn messages in shared chats.

   In a shared multi-user session a normal turn starts with ``[Name | Slack user <@U...>]``.
   A message that lands mid-turn is steered into the running turn instead, and the steer
   marker says it is "from the user", so it read as coming from whoever started the turn.
   Steered text now carries the same sender prefix, plus a line saying who it is from.
   Gateway-made notices (a finished background process, a goal or heartbeat wake-up) come from
   nobody, so neither path gives them a sender prefix; core would otherwise stamp them with the
   name of whoever spoke last.

6. Thread history uses the same mention format as live messages.

   The thread context fetched on a cold start or a re-tag kept raw ``<@UID>`` ids and dropped
   our own mention. Each line now reads ``@Name (UID)`` / ``@Name (you)`` like item 2.

7. A provider refusal pauses /goal with the real reason.

   When the judge's provider refuses the call (``finish_reason == "content_filter"``, as
   Anthropic does for some goals), core read the empty reply as "judge returned empty
   response", burned three turns, then blamed the judge model's JSON. The refusal is now a
   ``blocked`` verdict, so the goal pauses after the first turn and says the provider refused.

8. The session prompt names the current Slack channel id.

   Core's Slack session context says "DM with Casey" but never gives the channel id. The only
   ``D...`` id in the prompt was the home channel (the owner's DM), so a coworker's DM session
   read the owner's DM with the Slack tools, couldn't find the conversation, and quoted the
   owner's private messages back. The Slack platform notes now name this chat's channel id
   (and a real thread's ts) and say the home channel is a different chat.

9. Slack plan card: live tool progress that says nothing it shouldn't.

   With core's ``platforms.slack.extra.native_task_cards`` on, a tool-using turn on a message
   inside a thread shows a Slack plan block there. This plugin titles it "working on it", names
   each task by its kind ("checking slack", "running a query") instead of the tool name and its
   arguments, and marks a failed call as an error with no error text. Frames go out at most
   once a second, folding bursts together, and a task that finished after scrolling out of the
   card's last-eight window still gets its final state. A top-level message gets no card (the bot
   never opens a thread); the status line names its steps instead. A turn that ends silent
   (NO_REPLY) deletes its card, and a streaming error stays quiet instead of posting core's text
   progress fallback, unless ``tool_progress`` was set.

10. An edit to an old Slack message never posts "already received a different message".

   Slack sends an edit as the original message with new text. Core skips edits of messages it
   already handled, but only from an in-memory list that a gateway restart empties. An edit
   that lands after a restart then reaches admission under the original's id with other text,
   and core refuses it and posts a warning to the channel. The earlier message already ran, so
   on Slack that refusal now stays silent (gateway.log still records it).

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
import time

logger = logging.getLogger(__name__)

_ADAPTER_SUFFIX = os.path.join("plugins", "platforms", "slack", "adapter.py")
_PATCHED_FLAG = "_sme_mention_targets_patched"
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
# A thread copy read this recently is trusted without another conversations.replies call.
_FRESH_THREAD_SECONDS = 5.0


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


# ----- thread history mentions ---------------------------------------------------


def _make_thread_context_line(original):
    async def _thread_context_line(self, msg, msg_text, is_parent, team_id, channel_id, **kwargs):
        # Core strips our own mention before this runs. Put it back when that is the only
        # difference, so it can read "(you)", then humanize like a live message.
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id) or ""
        if bot_uid and isinstance(msg, dict):
            try:
                full = self._render_message_text(msg)
            except Exception:
                full = ""
            if full and full.replace(f"<@{bot_uid}>", "").strip() == msg_text:
                msg_text = full
        msg_text = await self._humanize_user_mentions(msg_text, chat_id=channel_id, team_id=team_id)
        return await original(self, msg, msg_text, is_parent, team_id, channel_id, **kwargs)
    _thread_context_line.__wrapped__ = original
    return _thread_context_line


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
    if _tagged_in_thread(adapter, thread_ts, team_id):
        return False
    return not await _bot_replied_in_thread(adapter, chat_id, thread_ts, team_id, refresh=True)


def _tagged_in_thread(adapter, thread_ts: str, team_id: str) -> bool:
    """True when someone tagged us in the thread during this run (core remembers it in memory).
    It covers a follow-up sent while our reply to that tag is still being written."""
    seen = getattr(adapter, "_mentioned_threads", None) or ()
    return adapter._workspace_message_marker(team_id, thread_ts) in seen or thread_ts in seen


def _mark_dropped_as_seen(adapter, event: dict, payload) -> None:
    """Record a dropped message's ts the way core's prefilter would have. Slack sends an edit
    that adds a mention twice (an app_mention under the original ts plus a message_changed);
    core lets only one through because the original ts is already seen. Dropping before core
    skipped that, so both copies ran and the second failed admission as a conflict."""
    ts = event.get("ts")
    if not ts:
        return
    try:
        adapter._dedup.is_duplicate(
            adapter._workspace_event_id(adapter._event_team_id(event, payload), ts))
    except Exception:
        logger.exception("slack-multiplayer-enhancements: could not mark a dropped message as seen")


def _make_handle_slack_message(original):
    async def _handle_slack_message(self, event, payload=None):
        if await _addressed_elsewhere(self, event):
            _mark_dropped_as_seen(self, event, payload)
            return None
        return await original(self, event, payload)
    _handle_slack_message.__wrapped__ = original
    return _handle_slack_message


# ----- wake on threads we posted in --------------------------------------------


async def _bot_replied_in_thread(adapter, channel_id: str, thread_ts: str, team_id: str = "",
                                 refresh: bool = False) -> bool:
    """True when any message in the thread was posted by this bot. Reads conversations.replies
    through the adapter's thread cache, fetching once on a miss. A cached copy can predate our
    own reply (it is saved when the thread is first read), so with ``refresh`` a cached "no"
    older than a few seconds is re-read from Slack before it counts."""
    if not thread_ts:
        return False
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id) or ""
    if not bot_uid:
        return False

    def cached():
        for key, entry in adapter._thread_context_cache.items():
            if key.startswith(f"{channel_id}:{thread_ts}:"):
                return entry
        return None

    entry = cached()
    if entry is not None:
        if any((m or {}).get("user") == bot_uid for m in (entry.messages or [])):
            return True
        if not refresh or time.monotonic() - entry.fetched_at < _FRESH_THREAD_SECONDS:
            return False
    await adapter._fetch_thread_context(channel_id=channel_id, thread_ts=thread_ts, current_ts="",
                                        team_id=team_id, force_refresh=entry is not None)
    entry = cached()
    return entry is not None and any(
        (m or {}).get("user") == bot_uid for m in (entry.messages or []))


def _make_should_wake(original):
    signature = inspect.signature(original)

    async def _should_wake_on_unmentioned_message(self, *args, **kwargs):
        if await original(self, *args, **kwargs):
            return True
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        a = bound.arguments
        return bool(a["is_thread_reply"] and await _bot_replied_in_thread(
            self, a["channel_id"], a["event_thread_ts"], a.get("team_id") or "", refresh=True))
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
    from gateway import session

    core_labels_sender = callable(getattr(session, "label_shared_sender", None))

    def _steer_text_with_origin(self, text, event):
        if text.strip() and event is not None:
            if getattr(event, "internal", False) and core_labels_sender:
                from dataclasses import replace
                from gateway.session_identity import replace_source

                event = replace(event, source=replace_source(event.source, user_name=None))
            elif not getattr(event, "internal", False):
                sender = _shared_sender_label(self, event.source)
                if sender:
                    if core_labels_sender:
                        return _SHARED_STEER_NOTE + original(self, text, event)
                    text = f"{_SHARED_STEER_NOTE}[{sender}] {text}"
        return original(self, text, event)
    _steer_text_with_origin.__wrapped__ = original
    return _steer_text_with_origin


def _make_prefix_inbound_sender_context(original):
    def _prefix_inbound_sender_context(self, event, source, message_text):
        if getattr(event, "internal", False) and getattr(source, "user_name", None):
            # A gateway-made notice has no sender; drop the name so no prefix is added. The
            # channel-context part of the original still applies.
            from gateway.session_identity import replace_source
            source = replace_source(source, user_name=None)
        return original(self, event, source, message_text)
    _prefix_inbound_sender_context.__wrapped__ = original
    return _prefix_inbound_sender_context


# ----- goal judge refusals ----------------------------------------------------------

_JUDGE_REFUSED_REASON = (
    "the judge model's provider refused to evaluate this goal (content filter), so it "
    "can't be judged")


def _make_call_goal_judge_llm(original):
    import json

    def _call_goal_judge_llm(call_llm, system_prompt, user_prompt, timeout):
        finish = []

        def _recording_call_llm(*args, **kwargs):
            resp = call_llm(*args, **kwargs)
            try:
                finish.append(resp.choices[0].finish_reason)
            except Exception:
                pass
            return resp

        raw = original(_recording_call_llm, system_prompt, user_prompt, timeout)
        if not (raw or "").strip() and "content_filter" in finish:
            return json.dumps({"verdict": "blocked", "reason": _JUDGE_REFUSED_REASON})
        return raw
    _call_goal_judge_llm.__wrapped__ = original
    return _call_goal_judge_llm


# ----- current channel id in the Slack session prompt --------------------------------


def _slack_channel_lines(context) -> list:
    src = getattr(context, "source", None)
    chat_id = str(getattr(src, "chat_id", "") or "")
    if not chat_id:
        return []
    lines = [f"**Current Slack channel id:** `{chat_id}`. Pass it as `channel_id` when you read "
             "this chat with the Slack tools. The home channel listed below is a different chat; "
             "never read it to answer someone here."]
    if str(getattr(src, "chat_type", "") or "") == "dm":
        lines.append("In this DM, top-level messages aren't threads: read context with "
                     "`conversations_history` on this channel id, and use `conversations_replies` "
                     "only when the message is a reply inside a real thread.")
    return lines


def _make_slack_platform_notes(original):
    def _slack_platform_notes(context):
        lines = list(original(context))
        try:
            lines.extend(_slack_channel_lines(context))
        except Exception:
            logger.exception("slack-multiplayer-enhancements: channel id note failed")
        return lines

    _slack_platform_notes.__wrapped__ = original
    return _slack_platform_notes


def _patch_session(module) -> None:
    notes = getattr(module, "_PLATFORM_NOTES", None)
    platform = getattr(getattr(module, "Platform", None), "SLACK", None)
    original = notes.get(platform) if isinstance(notes, dict) and platform is not None else None
    if original is None:
        logger.error("slack-multiplayer-enhancements: gateway.session._PLATFORM_NOTES[SLACK] is gone; "
                     "the channel id note is off")
        return
    if getattr(original, _PATCHED_FLAG, False):
        return
    patched = _make_slack_platform_notes(original)
    setattr(patched, _PATCHED_FLAG, True)
    notes[platform] = patched


def _patch_goals(module) -> None:
    original = getattr(module, "_call_goal_judge_llm", None)
    if original is None:
        logger.error("slack-multiplayer-enhancements: _call_goal_judge_llm is gone; the goal judge patch is off")
        return
    if getattr(original, _PATCHED_FLAG, False):
        return
    patched = _make_call_goal_judge_llm(original)
    setattr(patched, _PATCHED_FLAG, True)
    module._call_goal_judge_llm = patched


def _patch_run_inbound(module) -> None:
    cls = getattr(module, "GatewayInboundMixin", None)
    if cls is None:
        logger.error("slack-multiplayer-enhancements: GatewayInboundMixin is gone; the notice prefix patch is off")
        return
    if getattr(cls, _PATCHED_FLAG, False):
        return
    _wrap_method(cls, "_prefix_inbound_sender_context", _make_prefix_inbound_sender_context)
    setattr(cls, _PATCHED_FLAG, True)


# ----- Slack plan card ----------------------------------------------------------------

_PLAN_TITLE = "working on it"
# Slack allows chat.appendStream at Tier 4 (100+/min); one frame a second stays well under it.
_CARD_MIN_INTERVAL = 1.0
_CARD_TS_ATTR = "_sme_card_ts"
_CARD_LABELS = (
    (("send_message",), "sending a message"),
    (("slack",), "checking slack"),
    (("web_search", "search_web"), "searching the web"),
    (("web_extract", "browser", "browse", "fetch"), "reading a web page"),
    (("query", "sql", "snowflake", "autopat", "sigma"), "running a query"),
    (("terminal", "execute_code", "process", "shell"), "running a command"),
    (("write_file", "patch", "edit"), "editing files"),
    (("read_file", "search_files", "file"), "reading files"),
    (("session_search",), "looking through past chats"),
    (("skill", "memory"), "checking notes"),
    (("vision", "image"), "looking at an image"),
)


def _card_label(tool_name) -> str:
    """A short plain label for a tool's kind: never its name, arguments or results."""
    name = str(tool_name or "").lower()
    for needles, label in _CARD_LABELS:
        if any(n in name for n in needles):
            return label
    return "using a tool"


def _make_apply_event(original):
    def apply_event(self, raw):
        if isinstance(raw, dict) and raw.get("type") in {"tool.started", "tool.completed"}:
            raw = {"type": raw["type"], "tool_call_id": raw.get("tool_call_id"),
                   "tool_name": _card_label(raw.get("tool_name")), "is_error": raw.get("is_error")}
        return original(self, raw)
    apply_event.__wrapped__ = original
    return apply_event


def _make_task_card_publish(original):
    import asyncio

    async def _task_card_publish(self, st):
        # Coalesce: wait out the interval, then fold every queued event into one frame (each
        # frame carries the whole task list). The turn-end flush runs while cancelling: no wait.
        last = getattr(st, "_sme_last_publish", None)
        task = asyncio.current_task()
        cancelling = bool(task and task.cancelling())
        cancelled = None
        if last is not None and not cancelling:
            wait = last + _CARD_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError as exc:
                    # The turn ended mid-wait. This frame's events are already out of the
                    # queue, so core's own last flush won't see them: send them now.
                    cancelled = exc
                self._task_card_drain(st)
                if cancelled is not None and not (
                        self._ctx._run_still_current() and not self._agent_interrupted()):
                    raise cancelled
        st._sme_last_publish = time.monotonic()
        # Core shows the last 8 tasks. A task that finished between frames and already scrolled
        # out would keep its spinner forever, so send its final state once too.
        published = st.__dict__.setdefault("_sme_published", {})
        window = type(st).visible_tasks(st)
        shown = {task["id"] for task in window}
        settled = [st.tasks[i] for i in st.task_order
                   if i not in shown and published.get(i) != st.tasks[i]["status"]]
        frame = settled + window
        st.visible_tasks = lambda: frame
        try:
            result = await original(self, st)
        finally:
            del st.visible_tasks
            published.update({task["id"]: task["status"] for task in frame})
        if cancelled is not None:
            raise cancelled
        return result
    _task_card_publish.__wrapped__ = original
    return _task_card_publish


def _make_task_card_fallback(original):
    async def _task_card_send_or_edit_fallback(self, st):
        # Core turns a failed card into a text progress bubble. Without an operator-written
        # tool_progress mode that is new chatter in the channel, so stay quiet instead.
        if not getattr(self._ctx, "tool_progress_enabled", False):
            return None
        return await original(self, st)
    _task_card_send_or_edit_fallback.__wrapped__ = original
    return _task_card_send_or_edit_fallback


def _turn_was_silent(ctx) -> bool:
    try:
        from gateway.response_filters import is_intentional_silence_agent_result
        result = (getattr(ctx, "result_holder", None) or [None])[0]
        if not isinstance(result, dict):
            return False
        return bool(is_intentional_silence_agent_result(result, result.get("final_response")))
    except Exception:
        logger.exception("slack-multiplayer-enhancements: silent-turn check failed")
        return False


async def _drop_turn_cards(runner, adapter) -> None:
    """Forget this turn's cards; delete them when the turn ended silent (NO_REPLY)."""
    ctx = runner._ctx
    key_fn = getattr(adapter, "_native_task_card_key", None)
    cards = getattr(adapter, _CARD_TS_ATTR, None)
    if not callable(key_fn) or not isinstance(cards, dict):
        return
    key = key_fn(ctx.source.chat_id, ctx._progress_reply_to, ctx._progress_metadata)
    stamps = cards.pop(key, [])
    if not stamps or not _turn_was_silent(ctx):
        return
    client = adapter._get_client(key[1], team_id=key[0])
    for ts in stamps:
        try:
            await client.api_call("chat.delete", json={"channel": key[1], "ts": ts})
        except Exception as exc:
            logger.warning("slack-multiplayer-enhancements: couldn't delete a silent turn's card: %s", exc)


def _make_send_native_task_card_progress_runner(original):
    async def _send_native_task_card_progress(self, adapter):
        try:
            return await original(self, adapter)
        finally:
            try:
                await _drop_turn_cards(self, adapter)
            except Exception:
                logger.exception("slack-multiplayer-enhancements: card cleanup failed")
    _send_native_task_card_progress.__wrapped__ = original
    return _send_native_task_card_progress


def _make_send_native_task_card_progress(original):
    async def send_native_task_card_progress(self, chat_id, tasks, *, title=None, reply_to=None,
                                             metadata=None, fallback_text=None):
        result = await original(self, chat_id, tasks, title=_PLAN_TITLE, reply_to=reply_to,
                                metadata=metadata, fallback_text=fallback_text)
        stamp = getattr(result, "message_id", None) if getattr(result, "success", False) else None
        if stamp:
            key = self._native_task_card_key(chat_id, reply_to, metadata)
            cards = self.__dict__.setdefault(_CARD_TS_ATTR, {})
            seen = cards.setdefault(key, [])
            if stamp not in seen:
                seen.append(stamp)
        return result
    send_native_task_card_progress.__wrapped__ = original
    return send_native_task_card_progress


def _make_on_processing_start(original):
    async def on_processing_start(self, event):
        # The "is working" status under the message shows the turn instead of the eyes, in DMs and
        # threads. Slack shows no status under a top-level channel message, so that keeps the eyes.
        source = getattr(event, "source", None)
        if not _shows_status(str(getattr(source, "chat_id", "") or ""), getattr(source, "thread_id", None),
                             getattr(event, "message_id", None)):
            return await original(self, event)
        return None
    on_processing_start.__wrapped__ = original
    return on_processing_start


def _make_build_status_phrase(original):
    # The status line under a message is the turn's only progress view, so it names each step
    # the way the plan card did ("is running a command…"): never a tool name or its arguments.
    def build_status_phrase(tool_name, args=None, max_len=49):
        if not tool_name or tool_name == "_thinking":
            return original(tool_name, args, max_len)
        return f"is {_card_label(tool_name)}…"
    build_status_phrase.__wrapped__ = original
    return build_status_phrase


def _patch_display(module) -> None:
    original = getattr(module, "build_status_phrase", None)
    if original is None:
        logger.error("slack-multiplayer-enhancements: build_status_phrase is gone; status lines keep core's wording")
        return
    if getattr(original, "__wrapped__", None) is None:
        module.build_status_phrase = _make_build_status_phrase(original)


def _shows_status(chat_id, thread_id, message_id) -> bool:
    """Slack draws the status in a thread, and under a top-level DM message; not under a
    top-level channel message."""
    in_thread = bool(thread_id) and str(thread_id) != str(message_id)
    return in_thread or str(chat_id).startswith("D")


def _status_metadata_for_turn(self, chat_id, anchor, metadata):
    """Core's turn-status hook: with flat replies a top-level message names no thread, so core
    showed no status for it. Slack shows a status set on the message's own ts right under it,
    without opening a thread, so a message that would have got the eyes gets it there."""
    md = dict(metadata or {})
    if md.get("thread_id") or md.get("thread_ts") or self.config.extra.get("reply_in_thread", True) \
            or not _shows_status(chat_id, None, anchor):
        return md
    marker = self._workspace_message_marker(str(md.get("slack_team_id") or ""), str(anchor))
    if marker in self._reacting_message_ids:
        md["thread_id"] = str(anchor)
        anchors = self.__dict__.setdefault(_SESSION_STATUS_ATTR, {})
        anchors[(str(chat_id), str(anchor))] = True
        while len(anchors) > _SESSION_STATUS_MAX:
            anchors.pop(next(iter(anchors)))
    return md


# A text status (assistant.threads.setStatus) on a top-level DM message makes Slack desktop pop
# that message's thread panel open. The agent-session status shows a working line under it
# without the panel, but takes only a state, so top-level DM messages get that instead.
_SESSION_STATUS_ATTR = "_sme_session_status"
_SESSION_STATUS_MAX = 500


def _make_set_thread_status(original):
    async def _set_thread_status(self, chat_id, team_id, thread_ts, status, fail_label):
        if (str(chat_id), str(thread_ts)) not in self.__dict__.get(_SESSION_STATUS_ATTR, {}):
            return await original(self, chat_id, team_id, thread_ts, status, fail_label)
        try:
            await self._get_client(chat_id, team_id=team_id).api_call("agents.sessions.setStatus", json={
                "channel_id": chat_id, "thread_ts": thread_ts, "status": "processing" if status else "active"})
        except Exception as exc:
            logger.debug("slack-multiplayer-enhancements: agent session status %s: %s", fail_label, exc)
    _set_thread_status.__wrapped__ = original
    return _set_thread_status


def _make_session_status_method(original):
    # Core sends its free-text "is thinking..." status, which only assistant.threads.setStatus
    # takes; agents.sessions.setStatus (picked when the SDK has it) wants an enum and refuses
    # every call, so a thread never showed the status.
    def _session_status_method(client):
        legacy = getattr(client, "assistant_threads_setStatus", None)
        return legacy if legacy is not None else original(client)
    _session_status_method.__wrapped__ = original
    return _session_status_method


# A message that doesn't ask the bot (an untagged reply in a busy thread) still runs a turn, and
# core showed "is working" for it at once, which reads like a reply on the way. Such a turn holds
# the status back until its first tool starts; one that answers NO_REPLY never shows it.
_HELD_STATUS_ATTR = "_sme_held_status"
_HELD_STATUS_MAX = 500


def _status_key(chat_id, metadata):
    md = metadata or {}
    return str(chat_id), str(md.get("thread_id") or md.get("thread_ts") or "")


def _holds_status(event) -> bool:
    source = getattr(event, "source", None)
    return getattr(event, "reply_expected", None) is not True and getattr(source, "chat_type", "") != "dm"


def _make_start_typing_refresh(original):
    def _start_typing_refresh(self, event, interrupt_event, metadata):
        task = original(self, event, interrupt_event, metadata)
        # A key stays held until its status shows, so the clears after a silent turn send nothing.
        held = self.__dict__.setdefault(_HELD_STATUS_ATTR, {})
        key = _status_key(event.source.chat_id, metadata)
        held.pop(key, None)
        if task is not None and _holds_status(event):
            held[key] = True
            while len(held) > _HELD_STATUS_MAX:
                held.pop(next(iter(held)))
        return task
    _start_typing_refresh.__wrapped__ = original
    return _start_typing_refresh


def _make_send_typing(original):
    async def send_typing(self, chat_id, metadata=None):
        if _status_key(chat_id, metadata) in self.__dict__.get(_HELD_STATUS_ATTR, {}):
            return  # no tool has started yet
        return await original(self, chat_id, metadata)
    send_typing.__wrapped__ = original
    return send_typing


def _make_set_status_text(original):
    def set_status_text(self, chat_id, text):
        # A tool started: release this chat's held statuses (status text is per chat in core too).
        if text:
            held = self.__dict__.get(_HELD_STATUS_ATTR, {})
            for key in [k for k in held if k[0] == str(chat_id)]:
                held.pop(key, None)
        return original(self, chat_id, text)
    set_status_text.__wrapped__ = original
    return set_status_text


def _make_stop_typing(original):
    async def stop_typing(self, chat_id, metadata=None):
        if _status_key(chat_id, metadata) in self.__dict__.get(_HELD_STATUS_ATTR, {}):
            return  # never shown, nothing to clear
        return await original(self, chat_id, metadata)
    stop_typing.__wrapped__ = original
    return stop_typing


def _patch_adapter_module(module) -> None:
    original = getattr(module, "_session_status_method", None)
    if original is None:
        logger.error("slack-multiplayer-enhancements: _session_status_method is gone; the thread status patch is off")
        return
    if getattr(original, "__wrapped__", None) is None:
        module._session_status_method = _make_session_status_method(original)


def _patch_run_turn_progress(module) -> None:
    cls = getattr(module, "GatewayTurnProgressMixin", None)
    state = getattr(cls, "_TaskCardState", None)
    if cls is None or state is None:
        logger.error("slack-multiplayer-enhancements: the task-card rail is gone; the plan card patch is off")
        return
    if getattr(cls, _PATCHED_FLAG, False):
        return
    _wrap_method(state, "apply_event", _make_apply_event)
    _wrap_method(cls, "_task_card_publish", _make_task_card_publish)
    _wrap_method(cls, "_task_card_send_or_edit_fallback", _make_task_card_fallback)
    _wrap_method(cls, "_send_native_task_card_progress", _make_send_native_task_card_progress_runner)
    setattr(cls, _PATCHED_FLAG, True)


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
    cls.status_metadata_for_turn = _status_metadata_for_turn
    _wrap_method(cls, "_apply_bot_mention", _make_apply_bot_mention)
    _wrap_method(cls, "_build_identity_prompt", _make_build_identity_prompt)
    _wrap_method(cls, "_handle_slack_message", _make_handle_slack_message)
    _wrap_method(cls, "_should_wake_on_unmentioned_message", _make_should_wake)
    _wrap_method(cls, "_thread_context_line", _make_thread_context_line)
    _wrap_method(cls, "send_native_task_card_progress", _make_send_native_task_card_progress)
    _wrap_method(cls, "on_processing_start", _make_on_processing_start)
    _wrap_method(cls, "_start_typing_refresh", _make_start_typing_refresh)
    _wrap_method(cls, "send_typing", _make_send_typing)
    _wrap_method(cls, "stop_typing", _make_stop_typing)
    _wrap_method(cls, "set_status_text", _make_set_status_text)
    _wrap_method(cls, "_set_thread_status", _make_set_thread_status)
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


# ----- edit of an admitted message --------------------------------------------


def _is_slack_event(event) -> bool:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return str(getattr(platform, "value", platform) or "").lower() == "slack"


def _make_admit_message(original, module):
    async def admit_message(authority, event):
        reply = await original(authority, event)
        if reply is not None and reply == getattr(module, "ALREADY_ADMITTED_REPLY", None) \
                and _is_slack_event(event):
            logger.info("slack-multiplayer-enhancements: dropped an edit of already-admitted Slack "
                        "message %s quietly", getattr(event, "message_id", None))
            return None
        return reply
    setattr(admit_message, "__wrapped__", original)
    setattr(admit_message, "_sme_patched", True)
    return admit_message


def _patch_session_ingress(module) -> None:
    original = getattr(module, "admit_message", None)
    if original is None or not hasattr(module, "ALREADY_ADMITTED_REPLY"):
        logger.error("slack-multiplayer-enhancements: admit_message or ALREADY_ADMITTED_REPLY is gone; "
                     "the edit-conflict patch is off")
        return
    if getattr(original, "_sme_patched", False):
        return
    module.admit_message = _make_admit_message(original, module)


def _is_slack_adapter_file(path) -> bool:
    return bool(path) and os.path.normpath(str(path)).endswith(_ADAPTER_SUFFIX)


_GATEWAY_PATCHES = {
    "agent.display": _patch_display,
    "gateway.response_filters": _patch_response_filters,
    "gateway.run_busy": _patch_run_busy,
    "gateway.run_inbound": _patch_run_inbound,
    "gateway.run_turn_progress": _patch_run_turn_progress,
    "gateway.session": _patch_session,
    "gateway.session_ingress": _patch_session_ingress,
    "hermes_cli.goals": _patch_goals,
}


def _patch_module(module) -> None:
    name = getattr(module, "__name__", "")
    if name in _GATEWAY_PATCHES:
        _GATEWAY_PATCHES[name](module)
    elif _is_slack_adapter_file(getattr(module, "__file__", None)):
        _patch_adapter_class(getattr(module, "SlackAdapter", None))
        _patch_adapter_module(module)


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
