"""The Slack session prompt names the current chat's channel id, not only the home channel.

Run from a hermes-agent checkout with its dev environment (see README).
"""

import importlib.util
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _Ctx:
    def register_hook(self, name, cb):
        pass

    def on_unload(self, cb):
        pass


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "slack_multiplayer_session_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(_Ctx())


_load_plugin()

from gateway.config import HomeChannel, Platform  # noqa: E402
from gateway.session import SessionContext, SessionSource, build_session_context_prompt  # noqa: E402

COWORKER_DM = "D0COWORKER1"
OWNER_DM = "D0OWNERHOME"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))


def _prompt(source):
    home = HomeChannel(platform=Platform.SLACK, chat_id=OWNER_DM, name="owner dm")
    context = SessionContext(source=source, connected_platforms=[Platform.SLACK],
                             home_channels={Platform.SLACK: home})
    return build_session_context_prompt(context)


def test_coworker_dm_prompt_names_its_own_channel_id():
    prompt = _prompt(SessionSource(platform=Platform.SLACK, chat_id=COWORKER_DM, chat_type="dm",
                                   chat_name="Casey", user_id="U0CASEY", user_name="Casey"))

    assert f"**Current Slack channel id:** `{COWORKER_DM}`" in prompt
    assert "top-level messages aren't threads" in prompt


def test_channel_prompt_names_channel_without_dm_note():
    prompt = _prompt(SessionSource(platform=Platform.SLACK, chat_id="C0TEAM", chat_type="group",
                                   chat_name="team", user_id="U0CASEY", user_name="Casey"))

    assert "**Current Slack channel id:** `C0TEAM`" in prompt
    assert "top-level messages aren't threads" not in prompt
