# slack-multiplayer-enhancements

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for running a Hermes bot in a busy Slack channel shared with people and other bots.

## What it does

1. **Never posts the silence-marker warning.** When the bot answers a bare `NO_REPLY`, it stays silent, instead of the gateway posting "returned only a silence marker". This applies on every platform, DMs and @mentions included.
2. **Keeps who a mention targets.** Inbound `<@UID>` mentions reach the model as `@Name (UID)`, and a mention of the bot itself as `@Name (you)`, instead of a bare `@Name` with the bot's own mention removed. A bot whose display name is literally "bot" is no longer mistaken for this one. A short note in the identity prompt explains the format.
3. **Ignores messages meant for someone else.** A channel message that @mentions others but not the bot, or an untagged reply in a thread another bot started (and this bot never posted in), never reaches the bot. This matters when another bot in the workspace is named "Hermes": "@Hermes ..." no longer reads like a message for this one, and the identity prompt says that name never refers to the bot. DMs, the bot's own threads and messages that tag it are unchanged. A thread the bot was tagged in earlier counts as its own, so a follow-up there arrives even while the bot's answer is still being written. A dropped message still counts as seen, so an edit that adds the bot's mention starts one turn, not two (Slack sends that edit as two events).
4. **Wakes on replies in threads it posted in.** If the bot posted anywhere in a thread, even with a raw `chat.postMessage` under someone else's root, an untagged human reply there reaches it. The check reads the thread from Slack, so it survives restarts. A saved copy of the thread that says "not posted" is read again from Slack when it is more than a few seconds old, because it may predate the bot's own reply.
5. **Names the sender on mid-turn messages in shared chats.** In a shared multi-user session, a message steered into a running turn carries the same `[Name | Slack user <@U...>]` prefix a normal turn gets, plus a line saying it is from that sender, not necessarily whoever started the turn. Gateway-made notices (a finished background process, a goal or heartbeat wake-up) get no sender prefix on either path, so they no longer read as coming from whoever spoke last.
6. **Thread history shows mentions like live messages.** The thread context fetched on a cold start or a re-tag renders `<@UID>` as `@Name (UID)` and the bot's own mention as `@Name (you)`, instead of raw ids.
7. **A provider refusal pauses `/goal` with the real reason.** When the judge's provider refuses the call (`finish_reason: content_filter`), the goal pauses after the first turn saying the provider refused, instead of looping on "judge returned empty response" and then blaming the judge model's JSON.
8. **The session prompt names the current Slack channel id.** Core's Slack session context names the person but not the channel id, and the only DM id in the prompt was the home channel. A coworker's DM session then read the owner's DM with the Slack tools, found nothing, and quoted the owner's messages back. The Slack platform notes now give this chat's channel id, say the home channel is a different chat, and in DMs point at `conversations_history` instead of `conversations_replies` for top-level messages.

9. **A tidy Slack plan card, only inside threads.** With core's `platforms.slack.extra.native_task_cards: true`, a message already inside a thread gets Slack's plan block in that thread, titled "working on it". Each task is a plain label for the kind of tool ("checking slack", "searching the web", "running a query", "reading files"), never the tool name, its arguments or its output, and a failed call shows as an error with no error text. Updates go out at most once a second, and tasks that finish after scrolling out of view still end complete. The bot never opens a thread on its own, so a top-level message gets no card: its progress is the status line (item 11). A turn that ends in a silence marker deletes its card, and a Slack streaming error stays quiet instead of posting text progress, unless `tool_progress` is set. Turn it off with `hermes config set platforms.slack.extra.native_task_cards false` and a gateway restart.
10. **An edit to an old Slack message posts nothing.** Slack sends an edit as the original message with new text. Core skips edits of messages it already handled, but only from an in-memory list a gateway restart empties, so an edit after a restart was refused and the channel got "Hermes already received a different message under this message id". The earlier message already ran, so on Slack that refusal now stays silent; `gateway.log` still records it. Other platforms keep the notice.
11. **A step-by-step status line instead of the eyes.** In a DM or a thread, a message the bot takes up gets Slack's grey status line instead of the eyes reaction: in the thread, or right under a top-level DM message (without opening a thread). It names the running step the way the plan card does ("is running a command…", "is reading files…"), never a tool name or its arguments, says "is thinking..." between steps, and clears when the answer lands. Under a top-level DM message it is Slack's agent-session working line instead (no step text): a text status there makes Slack desktop pop that message's thread panel open, the session one doesn't. Slack draws no status under a top-level channel message, so those keep the eyes. The ✅ or ❌ still goes on when the turn ends.
12. **No "is working" status for messages the bot only overhears.** An untagged reply in a busy thread still runs a turn, so core showed "is working" on every one, which looks like a reply is coming. Such a turn now holds the status back until its first tool starts; one that ends in a silence marker shows nothing. Messages that tag the bot and DMs show it right away, as before.

Command parsing still uses the raw Slack text.

## Install

Copy this directory to `~/.hermes/plugins/slack-multiplayer-enhancements` and add it to `plugins.enabled` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - slack-multiplayer-enhancements
```

Then restart the gateway.

## Tests

Run from a hermes-agent checkout with its dev environment, so the real gateway and Slack adapter import:

```sh
cd <hermes-agent checkout> && .venv/bin/python -m pytest -p no:cacheprovider -q ~/.hermes/plugins/slack-multiplayer-enhancements/tests
```

Everything here patches private Hermes internals (`silence_allowed`, `GatewayBusySessionMixin._steer_text_with_origin`, `GatewayInboundMixin._prefix_inbound_sender_context`, `hermes_cli.goals._call_goal_judge_llm`, `GatewayTurnProgressMixin`'s `_TaskCardState.apply_event`, `_task_card_publish`, `_task_card_send_or_edit_fallback` and `_send_native_task_card_progress`, `gateway.session._PLATFORM_NOTES[Platform.SLACK]`, and `SlackAdapter`'s `_apply_bot_mention`, `_build_identity_prompt`, `_handle_slack_message`, `_should_wake_on_unmentioned_message`, `_thread_context_line`, `send_native_task_card_progress`, `on_processing_start`, `_start_typing_refresh`, `send_typing`, `stop_typing`, `set_status_text`, an added `status_metadata_for_turn`, `_set_thread_status`, plus the Slack adapter module's `_session_status_method`, `agent.display.build_status_phrase`, and `gateway.session_ingress.admit_message` / `ALREADY_ADMITTED_REPLY`). When upstream renames one, the plugin logs an error and that piece falls back to stock behavior, so rerun the tests after Hermes updates.
