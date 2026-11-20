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

9. **A tidy Slack plan card for live tool progress.** With core's `platforms.slack.extra.native_task_cards: true`, a turn shows Slack's plan block, titled "working on it". Each task is a plain label for the kind of tool ("checking slack", "searching the web", "running a query", "reading files"), never the tool name, its arguments or its output, and a failed call shows as an error with no error text. Updates go out at most once a second, and tasks that finish after scrolling out of view still end complete. The bot never opens a thread on its own. With flat replies (`reply_in_thread: false`), a top-level message that gets the eyes reaction (a DM, or a channel message that tags the bot) gets a flat card: a plain message with the plan, posted when the eyes go on and edited as tools run. Each card edits at most every 1.5 s, and edits across all turns are spaced 1.2 s apart to stay under Slack's `chat.update` limit in busy channels. The flat card keeps every step of the turn in order, each tool call and each thinking pause between them (core only sends its latest 8 at a time), and its last edit is retried if Slack refuses it. Every edit keeps the card's one `block_id`, so a card someone opened stays open; a new id per edit (as Slack's docs suggest) snaps it shut. When the answer lands, the card stays above it with its running steps marked done. A message already inside a thread keeps the card in that thread, and also shows Slack's "is thinking..." status there until the turn ends. Core asked for that status through `agents.sessions.setStatus`, which refuses free text, so the plugin sends it through `assistant.threads.setStatus`. A turn that ends in a silence marker (or fails before answering) deletes its card, retrying if Slack refuses the first delete, and a Slack streaming error stays quiet instead of posting text progress, unless `tool_progress` is set. Turn it off with `hermes config set platforms.slack.extra.native_task_cards false` and a gateway restart.
10. **An edit to an old Slack message posts nothing.** Slack sends an edit as the original message with new text. Core skips edits of messages it already handled, but only from an in-memory list a gateway restart empties, so an edit after a restart was refused and the channel got "Hermes already received a different message under this message id". The earlier message already ran, so on Slack that refusal now stays silent; `gateway.log` still records it. Other platforms keep the notice.

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

Everything here patches private Hermes internals (`silence_allowed`, `GatewayBusySessionMixin._steer_text_with_origin`, `GatewayInboundMixin._prefix_inbound_sender_context`, `hermes_cli.goals._call_goal_judge_llm`, `GatewayTurnMixin._run_agent_progress_threading`, `GatewayTurnProgressMixin`'s `_TaskCardState.apply_event`, `_task_card_publish`, `_task_card_send_or_edit_fallback` and `_send_native_task_card_progress`, `gateway.session._PLATFORM_NOTES[Platform.SLACK]`, and `SlackAdapter`'s `_apply_bot_mention`, `_build_identity_prompt`, `_handle_slack_message`, `_should_wake_on_unmentioned_message`, `_thread_context_line`, `send_native_task_card_progress`, `native_task_card_destination_supported`, `on_processing_start`, `on_processing_complete`, plus the Slack adapter module's `_session_status_method`, and `gateway.session_ingress.admit_message` / `ALREADY_ADMITTED_REPLY`). When upstream renames one, the plugin logs an error and that piece falls back to stock behavior, so rerun the tests after Hermes updates.
