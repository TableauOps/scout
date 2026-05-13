"""Slack webhook poster — used by Block 9 for notify_owner / flag_drift_for_owner.

Posts to the channel configured by SLACK_WEBHOOK_URL. Falls back to a console
panel when the webhook is unset, so the demo still renders without network.
"""
import json
import os
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

from rich.console import Console
from rich.panel import Panel

console = Console()


def webhook_configured() -> bool:
    return bool(os.environ.get("SLACK_WEBHOOK_URL"))


def _render_console_fallback(text: str, blocks: list | None) -> None:
    body = text
    if blocks:
        body += "\n\n[dim]blocks:[/dim]\n" + json.dumps(blocks, indent=2)
    console.print(Panel(
        body,
        title="[bold]Slack (console fallback — SLACK_WEBHOOK_URL unset)[/bold]",
        border_style="magenta",
    ))


def post_message(text: str, blocks: list | None = None) -> tuple[bool, str]:
    """Post to Slack. Returns (ok, info_string) for audit logging."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        _render_console_fallback(text, blocks)
        return True, "console-fallback (SLACK_WEBHOOK_URL not set)"

    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            status = resp.status
            return (200 <= status < 300), f"slack POST {status}"
    except HTTPError as e:
        return False, f"slack HTTPError {e.code}: {e.reason}"
    except URLError as e:
        return False, f"slack URLError: {e.reason}"


def build_owner_blocks(
    target_name: str,
    diagnosis: str,
    fix_steps: list[str],
    owner_mention: str | None = None,
    autonomy_note: str | None = None,
) -> list:
    """Compose Slack Block Kit blocks for an owner notification with fix steps."""
    # Plain-ASCII prefix; rich expands :colon_codes: in console fallback and
    # cp1252 on Windows can't render the resulting emoji char.
    header = f"*Scout flagged {target_name}*"
    if owner_mention:
        header += f" - {owner_mention}"

    blocks: list = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Diagnosis*\n{diagnosis}"}},
    ]
    if fix_steps:
        steps_md = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(fix_steps))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Direct steps to fix*\n{steps_md}"}})
    if autonomy_note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": autonomy_note}]})
    return blocks
