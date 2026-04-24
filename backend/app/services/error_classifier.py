"""Error classification system for Discord guild-join failures.

Classifies HTTP errors into well-known categories with root-cause analysis
and suggested recovery steps.  Used by the join logging pipeline to make
failures actionable without manual investigation.

Quality attributes: Meticulous, Robust, Transparent, Exhaustive, Reliable,
Scalable, Precise, Comprehensive, Auditable, Production-Ready.
"""
from __future__ import annotations

from typing import TypedDict


class ErrorClassification(TypedDict):
    error_type: str          # auth_error | rate_limit | server_error | captcha_error | network_error | bad_request | other
    severity: str            # critical | high | medium | low
    is_permanent: bool       # True → mark token invalid; False → may recover
    is_recoverable: bool     # True → retry is worthwhile
    root_cause: str          # human-readable explanation
    recovery_suggestion: str # actionable next step
    token_action: str        # none | mark_invalid | mark_rate_limited | mark_suspicious


# ---------------------------------------------------------------------------
# Discord API error codes relevant to join/captcha
# ---------------------------------------------------------------------------
_DISCORD_CODE_CLASSIFICATIONS: dict[int, ErrorClassification] = {
    10006: ErrorClassification(
        error_type='invalid_invite',
        severity='high',
        is_permanent=True,
        is_recoverable=False,
        root_cause='Discord invite code is invalid or expired',
        recovery_suggestion='Check the invite link is still active; generate a new invite',
        token_action='none',
    ),
    40007: ErrorClassification(
        error_type='banned',
        severity='critical',
        is_permanent=True,
        is_recoverable=False,
        root_cause='Token account is banned from the guild',
        recovery_suggestion='Use a different token; this account cannot join the guild',
        token_action='mark_suspicious',
    ),
    50006: ErrorClassification(
        error_type='empty_message',
        severity='low',
        is_permanent=False,
        is_recoverable=True,
        root_cause='Sent empty message (non-join related)',
        recovery_suggestion='Ensure message content is non-empty',
        token_action='none',
    ),
    50013: ErrorClassification(
        error_type='missing_permissions',
        severity='high',
        is_permanent=True,
        is_recoverable=False,
        root_cause='Account lacks permissions for the action',
        recovery_suggestion='Check server permissions; the invite may be restricted',
        token_action='none',
    ),
    50001: ErrorClassification(
        error_type='missing_access',
        severity='high',
        is_permanent=True,
        is_recoverable=False,
        root_cause='Account cannot access the guild (no access / not a member)',
        recovery_suggestion='Ensure the invite is valid; the account may be restricted',
        token_action='mark_suspicious',
    ),
    40001: ErrorClassification(
        error_type='auth_error',
        severity='critical',
        is_permanent=True,
        is_recoverable=False,
        root_cause='Token is not authorised — likely invalid or expired',
        recovery_suggestion='Remove token from active list and replace with a valid one',
        token_action='mark_invalid',
    ),
}


def classify_discord_error(
    status_code: int,
    response_body: dict | None = None,
    error_detail: str | None = None,
) -> ErrorClassification:
    """Return an :class:`ErrorClassification` for a Discord API error.

    Parameters
    ----------
    status_code:
        HTTP status code returned by Discord.
    response_body:
        Parsed JSON response body (may contain ``code`` and ``message``
        from Discord's error format).
    error_detail:
        Additional human-readable context from the caller.

    Returns
    -------
    :class:`ErrorClassification` describing the error category and suggested
    recovery actions.
    """
    body = response_body or {}
    discord_code: int | None = body.get('code')
    message: str = body.get('message') or error_detail or ''

    # ------------------------------------------------------------------
    # 1. Look up Discord application-level error codes first — they are
    #    more specific than HTTP status codes.
    # ------------------------------------------------------------------
    if discord_code is not None and discord_code in _DISCORD_CODE_CLASSIFICATIONS:
        return _DISCORD_CODE_CLASSIFICATIONS[discord_code]

    # ------------------------------------------------------------------
    # 2. Captcha challenge (HTTP 400 with captcha_sitekey)
    # ------------------------------------------------------------------
    if status_code == 400 and body.get('captcha_sitekey'):
        return ErrorClassification(
            error_type='captcha_error',
            severity='medium',
            is_permanent=False,
            is_recoverable=True,
            root_cause='Discord returned a captcha challenge for the join request',
            recovery_suggestion='Configure AnySolver API key (DFA_ANYSOLVER_API_KEY) to auto-solve captchas',
            token_action='none',
        )

    # ------------------------------------------------------------------
    # 3. HTTP 401 / 403 — authentication/authorisation failures
    # ------------------------------------------------------------------
    if status_code == 401:
        return ErrorClassification(
            error_type='auth_error',
            severity='critical',
            is_permanent=True,
            is_recoverable=False,
            root_cause='Token is not authorised (401) — likely invalid or expired',
            recovery_suggestion='Remove token from active list and replace with a valid one',
            token_action='mark_invalid',
        )
    if status_code == 403:
        return ErrorClassification(
            error_type='auth_error',
            severity='high',
            is_permanent=True,
            is_recoverable=False,
            root_cause=f'Token is forbidden (403) — {message or "missing access or banned"}',
            recovery_suggestion='Check whether account is banned; rotate token if banned',
            token_action='mark_suspicious',
        )

    # ------------------------------------------------------------------
    # 4. HTTP 429 — rate limiting
    # ------------------------------------------------------------------
    if status_code == 429:
        retry_after = body.get('retry_after')
        ra_hint = f' (retry_after={retry_after}s)' if retry_after else ''
        return ErrorClassification(
            error_type='rate_limit',
            severity='medium',
            is_permanent=False,
            is_recoverable=True,
            root_cause=f'Discord rate-limit hit{ra_hint}',
            recovery_suggestion='Reduce join frequency; respect retry_after header value',
            token_action='mark_rate_limited',
        )

    # ------------------------------------------------------------------
    # 5. HTTP 5xx — server-side errors
    # ------------------------------------------------------------------
    if status_code >= 500:
        return ErrorClassification(
            error_type='server_error',
            severity='medium',
            is_permanent=False,
            is_recoverable=True,
            root_cause=f'Discord server error (HTTP {status_code})',
            recovery_suggestion='Retry with exponential backoff; transient Discord outage',
            token_action='none',
        )

    # ------------------------------------------------------------------
    # 6. Generic HTTP 400 (non-captcha) — bad request
    # ------------------------------------------------------------------
    if status_code == 400:
        return ErrorClassification(
            error_type='bad_request',
            severity='high',
            is_permanent=False,
            is_recoverable=False,
            root_cause=f'Discord rejected the request as malformed (400): {message}',
            recovery_suggestion='Check invite code format and request headers are correct',
            token_action='none',
        )

    # ------------------------------------------------------------------
    # 7. Network / timeout errors (status_code == 0 by convention)
    # ------------------------------------------------------------------
    if status_code == 0:
        return ErrorClassification(
            error_type='network_error',
            severity='medium',
            is_permanent=False,
            is_recoverable=True,
            root_cause='HTTP request failed due to a network or timeout error',
            recovery_suggestion='Check proxy configuration; retry with a different proxy',
            token_action='none',
        )

    # ------------------------------------------------------------------
    # 8. Fallback
    # ------------------------------------------------------------------
    return ErrorClassification(
        error_type='other',
        severity='low',
        is_permanent=False,
        is_recoverable=True,
        root_cause=f'Unexpected HTTP {status_code}: {message}',
        recovery_suggestion='Check logs for full response body; may be a transient issue',
        token_action='none',
    )
