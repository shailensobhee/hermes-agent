from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_prompt_uses_visible_content_with_command_and_reason():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    command = "python scripts/deploy.py --env prod --force"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=command,
        session_key="discord:555",
        description="script execution via -c flag",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None

    prompt_text = sent["content"]
    assert "Command Approval Required" in prompt_text
    assert "Do you want Hermes to run this command?" in prompt_text
    assert "Requested command" in prompt_text
    assert command in prompt_text
    assert "Reason" in prompt_text
    assert "script execution via -c flag" in prompt_text


@pytest.mark.asyncio
async def test_exec_approval_prompt_splits_long_command_across_messages():
    """A long command must be shown in FULL, spread over several messages.

    Previously the content was hard-truncated with ``... [truncated]``, which
    asked the user to approve a command they could only partially read — a
    security footgun. The payload now spills into additional messages and the
    buttons hang off the last one.
    """
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    long_command = "python -c '" + ("x" * 5000) + "'"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=long_command,
        session_key="discord:555",
        description="long generated shell command",
    )

    assert result.success is True
    # Every individual message respects the platform cap...
    assert len(sent["content"]) <= adapter.MAX_MESSAGE_LENGTH
    # ...but nothing is clipped away.
    assert "... [truncated]" not in sent["content"]
    assert "long generated shell command" in sent["content"]
