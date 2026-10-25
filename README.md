# slack-multiplayer-enhancements

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for running a Hermes bot in a busy Slack channel shared with people and other bots.

## What it does

1. **Never posts the silence-marker warning.** When the bot answers a bare `NO_REPLY`, it stays silent, instead of the gateway posting "returned only a silence marker". This applies on every platform, DMs and @mentions included.
2. **Keeps who a mention targets.** Inbound `<@UID>` mentions reach the model as `@Name (UID)`, and a mention of the bot itself as `@Name (you)`, instead of a bare `@Name` with the bot's own mention removed. A bot whose display name is literally "bot" is no longer mistaken for this one. A short note in the identity prompt explains the format.
3. **Ignores messages meant for someone else.** A channel message that @mentions others but not the bot, or an untagged reply in a thread another bot started (and this bot never posted in), never reaches the bot. This matters when another bot in the workspace is named "Hermes": "@Hermes ..." no longer reads like a message for this one, and the identity prompt says that name never refers to the bot. DMs, the bot's own threads and messages that tag it are unchanged.
4. **Wakes on replies in threads it posted in.** If the bot posted anywhere in a thread, even with a raw `chat.postMessage` under someone else's root, an untagged human reply there reaches it. The check reads the thread from Slack, so it survives restarts.
5. **Names the sender on mid-turn messages in shared chats.** In a shared multi-user session, a message steered into a running turn carries the same `[Name | Slack user <@U...>]` prefix a normal turn gets, plus a line saying it is from that sender, not necessarily whoever started the turn.

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

Everything here patches private Hermes internals (`silence_allowed`, `GatewayBusySessionMixin._steer_text_with_origin`, and `SlackAdapter`'s `_apply_bot_mention`, `_build_identity_prompt`, `_handle_slack_message`, `_should_wake_on_unmentioned_message`). When upstream renames one, the plugin logs an error and that piece falls back to stock behavior, so rerun the tests after Hermes updates.
