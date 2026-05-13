"""Slack-message helpers for OSS Scout's notify_owner path.

Scout (OSS) only watches and calls in. This module renders plain-English
diagnosis + UI/desktop fix steps for the Slack message that goes to the
target's owner. No REST endpoints, no HTTP method names, no payload bodies.

The action arm — wire-format API calls, the credential broker, the executor —
lives in TableauOps Autopilot, not here.
"""


def fix_steps_for(plan: dict) -> list[str]:
    """Render concrete, owner-runnable steps for a recommendation. UI walkthrough
    or desktop-publish steps only — never REST mechanics."""
    action      = plan.get("action") or ""
    target_name = plan.get("target_name") or "(unknown target)"

    if action == "embed_credentials":
        return [
            f"Open *{target_name}* in Tableau Cloud → *Data Sources* → *Connections* tab.",
            "On each connection, click *Edit Connection*, check *Embedded password*, re-enter the username and password, and save.",
            "Trigger a verification refresh from the *Refresh Extracts* menu and confirm it succeeds.",
            "If you'd rather, re-publish the data source from Tableau Desktop with *Embed password* checked in the publish dialog.",
        ]

    if action == "retry_refresh":
        return [
            f"Open *{target_name}* in Tableau Cloud → *Refresh Extracts* → *Run Now*.",
            "If it succeeds, the original failure was transient and no further action is needed.",
            "If it fails again, open the *Connections* tab and look for an upstream error message (auth, network, or query).",
        ]

    if action == "approve_schedule_change":
        return [
            f"Confirm the new run cadence for *{target_name}* matches what you intended.",
            "If yes, no action needed — Scout will treat the new pattern as the baseline.",
            "If no, revert via the schedule UI in Tableau Cloud → *Tasks* → *Schedules*.",
        ]

    if action == "flag_drift_for_owner":
        return [
            f"Investigate why *{target_name}* is running off its usual schedule.",
            "Check recent commits / publishes that may have altered the task.",
            "If intended, re-run Scout with action `approve_schedule_change` to update the baseline.",
        ]

    if action == "notify_owner":
        return [
            f"Review *{target_name}*'s recent failures in Tableau Cloud → *Schedules* → *Run History*.",
            "Reach out to the publisher if the cause is upstream (DB/auth/etc.).",
        ]

    return [f"No prebuilt fix steps for action `{action}` — review the plan in scout.db audit_log."]


def build_owner_summary(plan: dict, history_snippet: str | None = None) -> str:
    """One-line diagnosis for the Slack message body."""
    parts = []
    if plan.get("reason"):
        parts.append(plan["reason"])
    if plan.get("confidence"):
        parts.append(f"_(confidence: {plan['confidence']})_")
    if history_snippet:
        parts.append(history_snippet)
    return " ".join(parts) or "Scout flagged this target for owner review."
