# slack-multiplayer-enhancements

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for running a Hermes bot in a busy Slack channel shared with people and other bots.

## What it does

1. **Stays quiet on untagged chatter.** A channel message that doesn't @mention the bot and isn't clearly addressed to it is marked as not needing a reply. When the bot answers `NO_REPLY` there, it stays silent instead of posting the "returned only a silence marker" warning. DMs and @mentions are unchanged.
2. **Keeps who a mention targets.** Inbound `<@UID>` mentions reach the model as `@Name (UID)`, and a mention of the bot itself as `@Name (you)`, instead of a bare `@Name` with the bot's own mention removed. A bot whose display name is literally "bot" is no longer mistaken for this one. A short note in the identity prompt explains the format.

3. **Ignores messages meant for someone else.** A channel message that @mentions others but not the bot, or an untagged reply in a thread another bot started, never reaches the bot. This matters when another bot in the workspace is named "Hermes": "@Hermes ..." no longer reads like a message for this one, and the identity prompt says that name never refers to the bot. DMs, the bot's own threads and messages that tag it are unchanged.

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

Run from a hermes-agent checkout so the real Slack adapter imports:

```sh
cd ~/.hermes/hermes-agent && python -m pytest -p no:cacheprovider ~/.hermes/plugins/slack-multiplayer-enhancements/tests -q
```

The mention override patches private `SlackAdapter` methods (`_humanize_user_mentions`, `_apply_bot_mention`, `_build_identity_prompt`, `_handle_slack_message`), so rerun the tests after Hermes updates.
