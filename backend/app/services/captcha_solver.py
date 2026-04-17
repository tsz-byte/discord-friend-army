from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.research import CaptchaChallenge

logger = logging.getLogger('discord_research.captcha_solver')
MAX_ERROR_LENGTH = 500
BACKOFF_MAX_EXPONENT = 4
# Default AnySolver endpoint — override with DFA_ANYSOLVER_BASE_URL if needed.
DEFAULT_ANYSOLVER_BASE_URL = 'https://api.anysolver.com'


class CaptchaSolverService:
    """AnySolver-only captcha solver for Discord hCaptcha challenges.

    Flow
    ----
    1. POST /createTask  → receive taskId
    2. Poll /getTaskResult with exponential back-off until status == 'ready'
    3. Extract gRecaptchaResponse (token) and rqtoken from the solution
    4. Return captcha_key / captcha_rqtoken / captcha_rqdata to the caller

    Discord-specific notes
    ----------------------
    * ``captcha_sitekey``  — hCaptcha sitekey embedded in the Discord 400 error payload
    * ``captcha_rqdata``   — extra token required by Discord; pass to AnySolver as both
                             ``rqdata`` and ``data`` fields in the task body
    * ``captcha_rqtoken``  — returned by AnySolver after solving; echo it back to Discord
                             alongside ``captcha_key`` in the retry request body
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key: str = settings.anysolver_api_key
        self.base_url: str = (settings.anysolver_base_url or DEFAULT_ANYSOLVER_BASE_URL).rstrip('/')
        # Task type sent to AnySolver. HCaptchaTaskProxyless is the correct type
        # for Discord's hCaptcha; override via DFA_CAPTCHA_TASK_TYPE if needed.
        self.task_type: str = settings.captcha_task_type
        self.poll_attempts: int = 20
        self.poll_base_delay_seconds: float = 2.0
        self.poll_max_delay_seconds: float = 8.0
        self.timeout_seconds: float = 30.0
        self.verify: bool | str = self._build_verify_value(settings)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def is_captcha_challenge(payload: dict | None) -> bool:
        """Return True when *payload* contains a Discord captcha challenge.

        Discord 400 error bodies always include ``captcha_sitekey`` when a
        captcha is required.  ``captcha_rqdata`` may be absent for some
        challenge variants and is therefore not required.
        """
        if not isinstance(payload, dict):
            return False
        return bool(payload.get('captcha_sitekey'))

    async def solve_discord_challenge(
        self,
        challenge_payload: dict,
        *,
        token_id: int | None = None,
        guild_id: str | None = None,
        user_agent: str,
        db: Session | None = None,
    ) -> dict:
        """Solve a Discord hCaptcha challenge via AnySolver.

        Parameters
        ----------
        challenge_payload:
            The raw JSON body Discord returned with a 400 captcha error.
        token_id:
            Database row-id of the account token that triggered the challenge.
        guild_id:
            Discord guild/server ID being joined.
        user_agent:
            User-Agent header used for the Discord invite request.
        db:
            Optional SQLAlchemy session.  When provided a ``CaptchaChallenge``
            audit row is written for every attempt.

        Returns
        -------
        dict with ``status`` key:
        * ``'ready'``  — ``captcha_key``, ``captcha_rqtoken``, ``captcha_rqdata``,
                         ``task_id``, ``cost_usd``, ``attempts``
        * ``'failed'`` — ``detail`` (human-readable error message)
        """
        if not self.is_enabled:
            return {
                'status': 'failed',
                'detail': 'AnySolver API key is not configured (set DFA_ANYSOLVER_API_KEY)',
            }
        if not self.is_captcha_challenge(challenge_payload):
            return {
                'status': 'failed',
                'detail': 'Discord response does not contain a captcha challenge',
            }

        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = challenge_payload.get('captcha_rqdata')
        challenge_row: CaptchaChallenge | None = None
        if db is not None:
            challenge_row = CaptchaChallenge(
                token_id=token_id,
                guild_id=guild_id,
                challenge_type=str(challenge_payload.get('captcha_service') or self.task_type),
                sitekey=sitekey,
                rqdata=str(rqdata) if rqdata is not None else None,
                solver_status='processing',
            )
            db.add(challenge_row)
            db.commit()
            db.refresh(challenge_row)

        logger.info(
            'AnySolver captcha solve start token_id=%s guild_id=%s sitekey=%.16s',
            token_id,
            guild_id,
            sitekey,
        )

        result = await self._solve(challenge_payload, user_agent=user_agent)

        if result.get('status') == 'ready':
            if challenge_row is not None:
                challenge_row.task_id = result.get('task_id')
                challenge_row.solver_status = 'ready'
                challenge_row.solved_token = str(result.get('captcha_key') or '')
                cost = result.get('cost_usd')
                challenge_row.cost_usd = str(cost) if cost is not None else None
                challenge_row.attempts = int(result.get('attempts') or 0)
                challenge_row.completed_at = datetime.now(timezone.utc)
                db.commit()
            logger.info(
                'AnySolver captcha solved task_id=%s attempts=%s cost=%s',
                result.get('task_id'),
                result.get('attempts'),
                result.get('cost_usd'),
            )
            return {
                'status': 'ready',
                'captcha_key': result.get('captcha_key'),
                'captcha_rqtoken': result.get('captcha_rqtoken'),
                'captcha_rqdata': result.get('captcha_rqdata'),
                'task_id': result.get('task_id'),
                'cost_usd': result.get('cost_usd'),
                'attempts': result.get('attempts'),
            }

        return await self._mark_failed(
            db,
            challenge_row,
            result.get('detail', 'AnySolver solve failed'),
            task_id=result.get('task_id'),
            attempts=result.get('attempts'),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _solve(self, challenge_payload: dict, *, user_agent: str) -> dict:
        """Low-level AnySolver createTask → getTaskResult polling loop."""
        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = challenge_payload.get('captcha_rqdata')
        session_id = challenge_payload.get('captcha_session_id')
        existing_rqtoken = challenge_payload.get('captcha_rqtoken')
        website_url = str(challenge_payload.get('captcha_website_url') or 'https://discord.com')

        # Build the AnySolver task body.
        # rqdata is sent as both 'rqdata' and 'data' for maximum provider
        # compatibility as noted in AnySolver's hCaptcha documentation.
        task_body: dict = {
            'type': self.task_type,
            'websiteURL': website_url,
            'websiteKey': sitekey,
            'userAgent': user_agent,
        }
        page_title = challenge_payload.get('captcha_page_title') or challenge_payload.get('pageTitle')
        if page_title:
            task_body['pageTitle'] = str(page_title)
        if challenge_payload.get('captcha_is_invisible') is not None:
            task_body['isInvisible'] = bool(challenge_payload.get('captcha_is_invisible'))
        if rqdata:
            rqdata_str = str(rqdata)
            task_body['rqdata'] = rqdata_str
            task_body['data'] = rqdata_str
        if session_id:
            task_body['sessionId'] = str(session_id)

        create_body = {'clientKey': self.api_key, 'task': task_body}

        async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=self.verify) as client:
            # --- Step 1: create task ---
            try:
                create_resp = await client.post(f'{self.base_url}/createTask', json=create_body)
                create_resp.raise_for_status()
            except httpx.HTTPError as exc:
                return {'status': 'failed', 'detail': f'AnySolver createTask request failed: {exc}'}

            create_data = self._safe_json(create_resp)
            if create_data.get('errorId') not in (None, 0):
                detail = (
                    create_data.get('errorDescription')
                    or create_data.get('errorCode')
                    or 'AnySolver createTask returned an error'
                )
                return {'status': 'failed', 'detail': str(detail)}

            task_id = create_data.get('taskId')
            if not task_id:
                return {'status': 'failed', 'detail': 'AnySolver createTask returned no taskId'}
            task_id = str(task_id)

            # --- Step 2: poll for result ---
            for attempt in range(1, self.poll_attempts + 1):
                try:
                    poll_resp = await client.post(
                        f'{self.base_url}/getTaskResult',
                        json={'clientKey': self.api_key, 'taskId': task_id},
                    )
                    poll_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    if attempt == self.poll_attempts:
                        return {
                            'status': 'failed',
                            'detail': f'AnySolver getTaskResult request failed: {exc}',
                            'task_id': task_id,
                            'attempts': attempt,
                        }
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue

                poll_data = self._safe_json(poll_resp)
                if poll_data.get('errorId') not in (None, 0):
                    detail = (
                        poll_data.get('errorDescription')
                        or poll_data.get('errorCode')
                        or 'AnySolver task returned an error'
                    )
                    return {'status': 'failed', 'detail': str(detail), 'task_id': task_id, 'attempts': attempt}

                status = poll_data.get('status')
                if status == 'processing':
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                if status == 'failed':
                    detail = (
                        poll_data.get('errorDescription')
                        or poll_data.get('errorCode')
                        or 'AnySolver reported task failed'
                    )
                    return {'status': 'failed', 'detail': str(detail), 'task_id': task_id, 'attempts': attempt}
                if status == 'ready':
                    solution = poll_data.get('solution') or {}
                    # AnySolver returns the hCaptcha solution token as
                    # ``gRecaptchaResponse`` (the primary key for all hCaptcha
                    # task types).  ``token`` and ``hcaptchaResponse`` are
                    # accepted as legacy / alternate field names to guard
                    # against future API shape changes.
                    token = (
                        solution.get('gRecaptchaResponse')
                        or solution.get('token')
                        or solution.get('hcaptchaResponse')
                    )
                    # rqtoken is required by Discord's hCaptcha validation.
                    rqtoken = solution.get('rqtoken') or existing_rqtoken
                    if not token:
                        return {
                            'status': 'failed',
                            'detail': 'AnySolver ready response is missing the solution token',
                            'task_id': task_id,
                            'attempts': attempt,
                        }
                    return {
                        'status': 'ready',
                        'captcha_key': token,
                        'captcha_rqtoken': rqtoken,
                        'captcha_rqdata': str(rqdata) if rqdata is not None else None,
                        'task_id': task_id,
                        'cost_usd': poll_data.get('cost'),
                        'attempts': attempt,
                    }
                # Unexpected status — do not spin forever.
                return {
                    'status': 'failed',
                    'detail': f'AnySolver returned unexpected status: {status!r}',
                    'task_id': task_id,
                    'attempts': attempt,
                }

            return {
                'status': 'failed',
                'detail': 'AnySolver task polling timed out',
                'task_id': task_id,
                'attempts': self.poll_attempts,
            }

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        try:
            text = response.text.strip()
        except Exception:
            text = ''
        return {'raw_text': text} if text else {}

    def _backoff_seconds(self, attempt: int) -> float:
        exponent = min(max(0, attempt - 1), BACKOFF_MAX_EXPONENT)
        return min(self.poll_max_delay_seconds, self.poll_base_delay_seconds * (2 ** exponent))

    def _build_verify_value(self, settings) -> bool | str:
        if not settings.captcha_ssl_verify:
            if str(settings.app_env).lower() in ('prod', 'production'):
                logger.error(
                    'AnySolver SSL certificate verification is disabled in production mode. '
                    'Re-enable DFA_CAPTCHA_SSL_VERIFY immediately.'
                )
            logger.warning(
                'AnySolver SSL certificate verification is disabled. '
                'Only use this setting for troubleshooting weak certificates.'
            )
            return False
        if settings.captcha_ca_bundle_path:
            logger.info('AnySolver using custom CA bundle: %s', settings.captcha_ca_bundle_path)
            return settings.captcha_ca_bundle_path
        return True

    async def _mark_failed(
        self,
        db: Session | None,
        challenge_row: CaptchaChallenge | None,
        detail: str,
        *,
        task_id: str | None = None,
        attempts: int | None = None,
    ) -> dict:
        if challenge_row is not None and db is not None:
            challenge_row.solver_status = 'failed'
            challenge_row.error = detail[:MAX_ERROR_LENGTH]
            if task_id is not None:
                challenge_row.task_id = task_id
            if attempts is not None:
                challenge_row.attempts = attempts
            challenge_row.completed_at = datetime.now(timezone.utc)
            db.commit()
        logger.warning('AnySolver captcha solve failed: %s', detail)
        return {'status': 'failed', 'detail': detail}
