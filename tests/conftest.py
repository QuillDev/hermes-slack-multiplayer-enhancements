# Load the real Slack libraries when installed, so a file that stubs them when missing doesn't
# hide them from tests that talk to a fake Slack over HTTP.
try:
    import aiohttp.web  # noqa: F401
    import slack_bolt.async_app  # noqa: F401
    import slack_sdk.web.async_client  # noqa: F401
except ImportError:
    pass
