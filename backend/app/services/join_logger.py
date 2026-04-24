"""Comprehensive join-attempt logging system for Discord guild joins.

Writes structured JSON audit files to ``important_req_logs/`` with four
sub-directories:

* ``join_attempts/``      — every POST /invites/{code} request + response
* ``captcha_challenges/`` — captcha challenge detection and analysis
* ``gateway_sessions/``   — WebSocket gateway session events and timing
* ``failures/``           — root-cause analysis for failed join attempts

Quality attributes: Meticulous, Robust, Transparent, Exhaustive, Reliable,
Scalable, Precise, Comprehensive, Auditable, Production-Ready.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger('discord_research.join_logger')

# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}')
_PROXY_CRED_RE = re.compile(r'(https?://)([^@]+)@')


def _mask_token(token: str) -> str:
    """Return a safely masked preview of a Discord token (first 4 + last 4 chars)."""
    if not token or len(token) < 10:
        return '****'
    return f'{token[:4]}...{token[-4:]}'


def _sanitize_headers(headers: dict) -> dict:
    """Return a copy of *headers* with credentials removed.

    Strips ``Authorization`` entirely and replaces values that look like
    Discord tokens with a masked version.  All other headers are kept intact
    so request fingerprints remain visible for debugging.
    """
    safe: dict = {}
    for k, v in (headers or {}).items():
        lower_k = k.lower()
        if lower_k == 'authorization':
            # Never write auth tokens to disk.
            safe[k] = '***'
        elif isinstance(v, str) and _TOKEN_RE.search(v):
            safe[k] = _mask_token(v)
        else:
            safe[k] = v
    return safe


def _sanitize_proxy(proxy_url: str | None) -> str | None:
    """Replace proxy username:password with ``user:****``."""
    if not proxy_url:
        return None
    return _PROXY_CRED_RE.sub(lambda m: f'{m.group(1)}user:****@', proxy_url)


def _sanitize_body(body: dict) -> dict:
    """Return a copy of *body* with credential fields masked."""
    if not isinstance(body, dict):
        return body
    safe: dict = {}
    for k, v in body.items():
        if isinstance(v, str) and _TOKEN_RE.search(v):
            safe[k] = _mask_token(v)
        else:
            safe[k] = v
    return safe


# ---------------------------------------------------------------------------
# Core logger class
# ---------------------------------------------------------------------------

class JoinLogger:
    """Meticulous, exhaustive logger for Discord guild-join audit trails.

    Creates and manages the ``important_req_logs/`` directory hierarchy.
    All public methods are *best-effort*: they catch and log any I/O error
    so that a logging failure never breaks the join flow.
    """

    def __init__(self, log_dir: str | Path = 'important_req_logs') -> None:
        self.log_dir = Path(log_dir)
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Directory helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for sub in ('join_attempts', 'captcha_challenges', 'gateway_sessions', 'failures'):
            try:
                (self.log_dir / sub).mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # pragma: no cover
                logger.warning('JoinLogger: failed to create directory %s/%s: %s', self.log_dir, sub, exc)

    # ------------------------------------------------------------------
    # Correlation ID factory
    # ------------------------------------------------------------------

    @staticmethod
    def new_correlation_id() -> str:
        """Return a fresh UUID4 correlation ID for one join attempt."""
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    def log_join_attempt(
        self,
        *,
        correlation_id: str,
        token_id: int | None,
        token_preview: str,
        invite_code: str,
        attempt_number: int,
        request_method: str,
        request_url: str,
        request_headers: dict,
        request_body: dict,
        response_status: int,
        response_headers: dict,
        response_body: Any,
        proxy_url: str | None = None,
        session_id: str | None = None,
        fingerprint_validation: dict | None = None,
        gateway_used: bool = False,
        gateway_fallback: bool = False,
        elapsed_ms: float | None = None,
        notes: str = '',
    ) -> None:
        """Write a complete join-attempt audit record.

        Captures full request and response for every POST /invites/{code}
        call regardless of whether it succeeded or failed.  Authorization
        headers are never written to disk.
        """
        ts = datetime.now(timezone.utc).isoformat()
        filename = (
            f'join_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}'
            f'_cid_{correlation_id[:8]}'
            f'_token_{token_id}'
            f'_invite_{invite_code}'
            f'_attempt_{attempt_number}.json'
        )
        record: dict = {
            'correlation_id': correlation_id,
            'timestamp': ts,
            'token_id': token_id,
            'token_preview': token_preview,
            'invite_code': invite_code,
            'attempt_number': attempt_number,
            'elapsed_ms': elapsed_ms,
            'proxy': _sanitize_proxy(proxy_url),
            'session_id': session_id,
            'gateway_used': gateway_used,
            'gateway_fallback': gateway_fallback,
            'fingerprint_validation': fingerprint_validation or {},
            'request': {
                'method': request_method,
                'url': request_url,
                'headers': _sanitize_headers(request_headers),
                'body': _sanitize_body(request_body),
            },
            'response': {
                'status_code': response_status,
                'headers': dict(response_headers or {}),
                'body': response_body,
            },
            'notes': notes,
        }
        self._write(self.log_dir / 'join_attempts', filename, record)

    def log_captcha_challenge(
        self,
        *,
        correlation_id: str,
        token_id: int | None,
        invite_code: str,
        attempt_number: int,
        challenge_payload: dict,
        solve_attempted: bool = False,
        solve_service: str = 'AnySolver',
        solve_task_id: str | None = None,
        solve_status: str | None = None,
        solve_solution_token_preview: str | None = None,
        solve_context_id_empty: bool | None = None,
        solve_elapsed_ms: float | None = None,
        notes: str = '',
    ) -> None:
        """Write a captcha-challenge analysis record.

        Validates which challenge fields are present and captures the full
        challenge payload (sitekey is safe to log; rqdata may contain
        discriminating information and is kept).
        """
        sitekey = challenge_payload.get('captcha_sitekey') or ''
        rqdata = challenge_payload.get('captcha_rqdata')
        rqtoken = challenge_payload.get('captcha_rqtoken')
        session_id = challenge_payload.get('captcha_session_id')
        service = challenge_payload.get('captcha_service')
        ts = datetime.now(timezone.utc).isoformat()
        filename = (
            f'captcha_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}'
            f'_cid_{correlation_id[:8]}'
            f'_token_{token_id}'
            f'_invite_{invite_code}'
            f'_attempt_{attempt_number}.json'
        )
        record: dict = {
            'correlation_id': correlation_id,
            'timestamp': ts,
            'token_id': token_id,
            'invite_code': invite_code,
            'attempt_number': attempt_number,
            'challenge_analysis': {
                'sitekey_present': bool(sitekey),
                'rqdata_present': rqdata is not None,
                'rqtoken_present': rqtoken is not None,
                'session_id_present': session_id is not None,
                'service_field': service,
                'field_completeness': _captcha_field_completeness(challenge_payload),
            },
            'challenge_payload': challenge_payload,
            'solve': {
                'attempted': solve_attempted,
                'service': solve_service,
                'task_id': solve_task_id,
                'status': solve_status,
                'solution_token_preview': solve_solution_token_preview,
                'context_id_empty': solve_context_id_empty,
                'elapsed_ms': solve_elapsed_ms,
            },
            'notes': notes,
        }
        self._write(self.log_dir / 'captcha_challenges', filename, record)

    def log_gateway_session(
        self,
        *,
        correlation_id: str,
        token_id: int | None,
        invite_code: str,
        event: str,
        session_id: str | None = None,
        heartbeat_interval_ms: float | None = None,
        connect_elapsed_ms: float | None = None,
        ready_elapsed_ms: float | None = None,
        used_fallback: bool = False,
        error: str | None = None,
        notes: str = '',
    ) -> None:
        """Write a gateway-session event record.

        Events: ``connect_start``, ``hello_received``, ``identify_sent``,
        ``ready_received``, ``timeout``, ``error``, ``fallback_used``.
        """
        ts = datetime.now(timezone.utc).isoformat()
        filename = (
            f'gw_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}'
            f'_cid_{correlation_id[:8]}'
            f'_token_{token_id}'
            f'_{event}.json'
        )
        record: dict = {
            'correlation_id': correlation_id,
            'timestamp': ts,
            'token_id': token_id,
            'invite_code': invite_code,
            'event': event,
            'session_id': session_id,
            'heartbeat_interval_ms': heartbeat_interval_ms,
            'connect_elapsed_ms': connect_elapsed_ms,
            'ready_elapsed_ms': ready_elapsed_ms,
            'used_fallback': used_fallback,
            'error': error,
            'notes': notes,
        }
        self._write(self.log_dir / 'gateway_sessions', filename, record)

    def log_failure(
        self,
        *,
        correlation_id: str,
        token_id: int | None,
        invite_code: str,
        error_type: str,
        severity: str,
        is_permanent: bool,
        is_recoverable: bool,
        root_cause: str,
        recovery_suggestion: str,
        token_action: str,
        http_status: int | None = None,
        response_body: Any = None,
        attempt_number: int | None = None,
        notes: str = '',
    ) -> None:
        """Write a root-cause failure analysis record to ``failures/``."""
        ts = datetime.now(timezone.utc).isoformat()
        filename = (
            f'failure_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}'
            f'_cid_{correlation_id[:8]}'
            f'_token_{token_id}'
            f'_invite_{invite_code}.json'
        )
        record: dict = {
            'correlation_id': correlation_id,
            'timestamp': ts,
            'token_id': token_id,
            'invite_code': invite_code,
            'attempt_number': attempt_number,
            'error_classification': {
                'error_type': error_type,
                'severity': severity,
                'is_permanent': is_permanent,
                'is_recoverable': is_recoverable,
                'root_cause': root_cause,
                'recovery_suggestion': recovery_suggestion,
                'token_action': token_action,
            },
            'http_status': http_status,
            'response_body': response_body,
            'notes': notes,
        }
        self._write(self.log_dir / 'failures', filename, record)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, directory: Path, filename: str, record: dict) -> None:
        """Serialise *record* to *directory*/*filename* as pretty-printed JSON.

        Errors are caught and logged at WARNING level so that a logging
        failure never propagates to the join flow.
        """
        try:
            directory.mkdir(parents=True, exist_ok=True)
            filepath = directory / filename
            filepath.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str),
                encoding='utf-8',
            )
            logger.debug('JoinLogger wrote %s', filepath)
        except Exception as exc:  # pragma: no cover
            logger.warning('JoinLogger: failed to write %s/%s: %s', directory, filename, exc)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _captcha_field_completeness(payload: dict) -> str:
    """Return a human-readable completeness grade for a captcha challenge payload."""
    fields = {
        'captcha_sitekey': payload.get('captcha_sitekey'),
        'captcha_rqdata': payload.get('captcha_rqdata'),
        'captcha_rqtoken': payload.get('captcha_rqtoken'),
        'captcha_session_id': payload.get('captcha_session_id'),
    }
    present = [k for k, v in fields.items() if v]
    total = len(fields)
    score = len(present)
    grade = {4: 'complete', 3: 'partial-high', 2: 'partial-low', 1: 'minimal', 0: 'empty'}.get(score, 'unknown')
    return f'{grade} ({score}/{total} fields: {present})'


# ---------------------------------------------------------------------------
# Timing context manager
# ---------------------------------------------------------------------------

class _Timer:
    """Simple elapsed-time helper."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000


def start_timer() -> '_Timer':
    """Return a running :class:`_Timer` for measuring elapsed request time."""
    return _Timer()
