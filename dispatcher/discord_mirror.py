"""Discord mirror for Disco-Bus.

Posts each delivered envelope to:
  global channel             — firehose of all traffic
  sender's per-agent channel — if mapped in the channel registry
  recipient's per-agent channel — same mapping

Long bodies are pretty-printed as fenced JSON, paged across multiple Discord
messages (Discord limit: 2000 chars/message).

Fire-and-forget: never raises, never blocks delivery.

Configuration:
  Bot token:      DISCOBUS_DISCORD_TOKEN_FILE  (default ~/.disco-bus/discord-token)
  Channel map:    DISCOBUS_CHANNELS            (default ~/.disco-bus/discord-channels.json)

Channel map JSON shape:
  {
    "global": <channel_id>,
    "agents": { "AgentName": <channel_id>, ... }
  }

If the token file is absent, mirroring is silently disabled.
"""

import json
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger("disco-bus.discord")

TOKEN_FILE = Path(
    os.environ.get("DISCOBUS_DISCORD_TOKEN_FILE", str(Path.home() / ".disco-bus" / "discord-token"))
)
CHANNELS_FILE = Path(
    os.environ.get("DISCOBUS_CHANNELS", str(Path.home() / ".disco-bus" / "discord-channels.json"))
)
DISCORD_API = "https://discord.com/api/v10"
POST_TIMEOUT_SEC = 5
DISCORD_MAX = 2000
PAGE_BUDGET = 1900  # leave headroom for code-fence markers

_token: str | None = None
_global_channel: int | None = None
_agent_channels: dict[str, int] | None = None


def _load() -> bool:
    global _token, _global_channel, _agent_channels
    if _token is None:
        try:
            _token = TOKEN_FILE.read_text().strip()
        except OSError as e:
            log.warning(f"discord mirror disabled (token): {e}")
            return False
    if _agent_channels is None:
        try:
            data = json.loads(CHANNELS_FILE.read_text())
            _global_channel = data.get("global")
            _agent_channels = data.get("agents", {})
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"discord mirror disabled (channels): {e}")
            return False
    return True


def _format_body(body) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, indent=2, ensure_ascii=False)
    return str(body)


def _pages(env: dict) -> list[str]:
    """Render envelope as Discord-sized message pages."""
    header = f"**{env['from']}→{env['to']}** `#{env['id']}` `{env['subject']}`"
    body = env.get("body")
    body_text = _format_body(body)
    is_structured = isinstance(body, (dict, list))

    if not body_text:
        return [header[:DISCORD_MAX]]

    fence_open = "```json\n" if is_structured else ""
    fence_close = "\n```" if is_structured else ""
    fence_overhead = len(fence_open) + len(fence_close)

    pages: list[str] = []
    cursor = 0
    body_len = len(body_text)
    first = True
    while cursor < body_len:
        head = (header + "\n") if first else ""
        budget = PAGE_BUDGET - len(head) - fence_overhead
        if budget < 200:
            pages.append(head.rstrip())
            first = False
            continue
        chunk = body_text[cursor:cursor + budget]
        if cursor + budget < body_len:
            nl = chunk.rfind("\n")
            if nl > budget // 2:
                chunk = chunk[:nl]
        pages.append(f"{head}{fence_open}{chunk}{fence_close}")
        cursor += len(chunk)
        first = False
    return pages


def _post(channel_id: int, content: str) -> None:
    try:
        r = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {_token}"},
            json={"content": content[:DISCORD_MAX]},
            timeout=POST_TIMEOUT_SEC,
        )
        if r.status_code >= 400:
            log.warning(f"discord post chan={channel_id} {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"discord post chan={channel_id}: {e}")


def mirror(envelope: dict) -> None:
    try:
        if not _load():
            return
        target_ids: set[int] = set()
        if _global_channel:
            target_ids.add(_global_channel)
        for agent in (envelope.get("from"), envelope.get("to")):
            if isinstance(agent, str) and _agent_channels:
                cid = _agent_channels.get(agent)
                if cid:
                    target_ids.add(cid)
        pages = _pages(envelope)
        for cid in target_ids:
            for page in pages:
                _post(cid, page)
    except Exception as e:
        log.warning(f"discord mirror crashed: {e}")
