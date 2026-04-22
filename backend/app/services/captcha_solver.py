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
    """AnySolver-only captcha solver for Discord PopularCaptcha (hCaptcha) challenges.

    Flow
    ----
    1. Create browser session via PopularPlatformSessionAction (sessionType=discord)
    2. Create PopularCaptcha* task with sessionId + Discord challenge fields
    3. Poll /getTaskResult with exponential back-off until status == 'ready'
    4. Extract ``token`` (primary) from the solution and ``rqtoken`` if present
    5. Return captcha_key / captcha_rqtoken / captcha_rqdata to the caller

    Discord-specific notes
    ----------------------
    * Discord uses enterprise invisible hCaptcha, solved via AnySolver task type
      ``PopularCaptchaEnterpriseInvisibleTokenProxyLess`` (configurable).
    * ``captcha_sitekey``  — sitekey embedded in the Discord 400 error payload
    * ``captcha_rqdata``   — extra token required by Discord; pass to AnySolver as ``rqdata``
    * ``captcha_rqtoken``  — returned by AnySolver (or echoed from Discord); sent back to
                             Discord alongside ``captcha_key`` in the retry request body

    AnySolver task body fields (PopularCaptcha* types)
    ---------------------------------------------------
    Required: type, websiteURL, websiteKey
    Optional: rqdata, sessionId, proxy
    Solution response: solution.token

    Top-level createTask body fields
    ---------------------------------
    Required: clientKey, task
    Optional: provider  (e.g. "EZCaptcha" — set DFA_CAPTCHA_PROVIDER to enable)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key: str = settings.anysolver_api_key
        self.base_url: str = (settings.anysolver_base_url or DEFAULT_ANYSOLVER_BASE_URL).rstrip('/')
        # Task type sent to AnySolver.
        # Discord requires PopularCaptchaEnterpriseInvisibleTokenProxyLess.
        self.task_type: str = settings.captcha_task_type
        # Optional provider forwarded in every createTask body (e.g. "EZCaptcha").
        self.provider: str = settings.captcha_provider or ''
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
        proxy_url: str | None = None,
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
        proxy_url:
            Optional proxy URL (``http://user:pass@host:port``) to include in
            the captcha task body so AnySolver uses the same outbound IP as the
            Discord join request.  When *None* the task is solved proxyless.
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

        result = await self._solve(challenge_payload, user_agent=user_agent, proxy_url=proxy_url)

        if result.get('status') == 'ready':
            if challenge_row is not None:
                challenge_row.task_id = result.get('task_id')
                challenge_row.anysolver_session_id = result.get('anysolver_session_id')
                challenge_row.solver_status = 'ready'
                challenge_row.solved_token = str(result.get('captcha_key') or '')
                cost = result.get('cost_usd')
                challenge_row.cost_usd = str(cost) if cost is not None else None
                challenge_row.attempts = int(result.get('attempts') or 0)
                challenge_row.completed_at = datetime.now(timezone.utc)
                db.commit()
            logger.info(
                'AnySolver captcha solved task_id=%s session_id=%s attempts=%s cost=%s',
                result.get('task_id'),
                result.get('anysolver_session_id'),
                result.get('attempts'),
                result.get('cost_usd'),
            )
            return {
                'status': 'ready',
                'captcha_key': result.get('captcha_key'),
                'captcha_rqtoken': result.get('captcha_rqtoken'),
                'captcha_rqdata': result.get('captcha_rqdata'),
                'task_id': result.get('task_id'),
                'anysolver_session_id': result.get('anysolver_session_id'),
                'cost_usd': result.get('cost_usd'),
                'attempts': result.get('attempts'),
            }

        return await self._mark_failed(
            db,
            challenge_row,
            result.get('detail', 'AnySolver solve failed'),
            task_id=result.get('task_id'),
            anysolver_session_id=result.get('anysolver_session_id'),
            attempts=result.get('attempts'),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _solve(self, challenge_payload: dict, *, user_agent: str, proxy_url: str | None = None) -> dict:
        """Low-level AnySolver session-create + captcha-create/poll flow."""
        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = challenge_payload.get('captcha_rqdata')
        existing_rqtoken = challenge_payload.get('captcha_rqtoken')
        website_url = str(challenge_payload.get('captcha_website_url') or 'https://discord.com')
        session_result = await self._create_session()
        if session_result.get('status') != 'ready':
            if session_result.get('detail'):
                session_result['detail'] = f"Session creation failed: {session_result.get('detail')}"
            return session_result
        anysolver_session_id = str(session_result.get('session_id') or '')
        anysolver_user_agent = session_result.get('user_agent') or user_agent
        logger.info(
            'AnySolver session created session_id=%s user_agent=%s',
            anysolver_session_id,
            str(anysolver_user_agent),
        )

        # Build the AnySolver PopularCaptcha* task body.
        # AnySolver's PopularCaptcha task types accept: type, websiteURL,
        # websiteKey, rqdata (optional), sessionId, proxy (optional).
        # Do NOT include userAgent, isInvisible, data, or pageTitle — those are
        # not valid fields for PopularCaptcha* task types.
        task_body: dict = {
            'type': self.task_type,
            'websiteURL': website_url,
            'websiteKey': sitekey,
            'sessionId': anysolver_session_id,
        }
        if rqdata:
            task_body['rqdata'] = str(rqdata)
        if proxy_url:
            task_body['proxy'] = proxy_url
        poll_result = await self._create_task_and_poll(task_body, purpose='captcha')
        if poll_result.get('status') != 'ready':
            poll_result['anysolver_session_id'] = anysolver_session_id
            return poll_result

        solution = poll_result.get('solution') or {}
        # AnySolver returns the solved token as ``token`` for all
        # PopularCaptcha* task types.  ``gRecaptchaResponse`` is
        # accepted as a fallback for any future API shape changes.
        token = solution.get('token') or solution.get('gRecaptchaResponse')
        # rqtoken is required by Discord's challenge validation.
        rqtoken = solution.get('rqtoken') or existing_rqtoken
        if not token:
            return {
                'status': 'failed',
                'detail': 'AnySolver ready response is missing the solution token',
                'task_id': poll_result.get('task_id'),
                'anysolver_session_id': anysolver_session_id,
                'attempts': poll_result.get('attempts'),
            }
        return {
            'status': 'ready',
            'captcha_key': token,
            'captcha_rqtoken': rqtoken,
            'captcha_rqdata': str(rqdata) if rqdata is not None else None,
            'task_id': poll_result.get('task_id'),
            'anysolver_session_id': anysolver_session_id,
            'cost_usd': poll_result.get('cost_usd'),
            'attempts': poll_result.get('attempts'),
        }

    async def _create_session(self) -> dict:
        """Create AnySolver Discord browser session (required before captcha tasks)."""
        session_task = {'type': 'PopularPlatformSessionAction', 'sessionType': 'discord'}
        result = await self._create_task_and_poll(session_task, purpose='session')
        if result.get('status') != 'ready':
            return result
        solution = result.get('solution') or {}
        session_id = solution.get('sessionId')
        user_agent = solution.get('userAgent')
        if not session_id:
            return {
                'status': 'failed',
                'detail': 'AnySolver session response is missing solution.sessionId',
                'task_id': result.get('task_id'),
                'attempts': result.get('attempts'),
            }
        return {
            'status': 'ready',
            'session_id': str(session_id),
            'user_agent': str(user_agent) if user_agent else None,
            'task_id': result.get('task_id'),
            'cost_usd': result.get('cost_usd'),
            'attempts': result.get('attempts'),
        }

    async def _create_task_and_poll(self, task_body: dict, *, purpose: str) -> dict:
        """Create AnySolver task and poll until ready/failed/timeout."""
        create_body: dict = {'clientKey': self.api_key, 'task': task_body}
        if self.provider:
            create_body['provider'] = self.provider
        logger.info('AnySolver %s task create start type=%s', purpose, task_body.get('type'))
        async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=self.verify) as client:
            try:
                create_resp = await client.post(f'{self.base_url}/createTask', json=create_body)
                create_resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning('AnySolver %s createTask HTTP error: %s', purpose, exc)
                return {'status': 'failed', 'detail': f'AnySolver createTask request failed: {exc}'}

            create_data = self._safe_json(create_resp)
            if create_data.get('errorId') not in (None, 0):
                detail = (
                    create_data.get('errorDescription')
                    or create_data.get('errorCode')
                    or 'AnySolver createTask returned an error'
                )
                logger.warning('AnySolver %s createTask API error: %s payload=%s', purpose, detail, create_data)
                return {'status': 'failed', 'detail': str(detail)}
            create_status = create_data.get('status')
            if create_status == 'ready':
                logger.info('AnySolver %s task ready from createTask response', purpose)
                return {
                    'status': 'ready',
                    'solution': create_data.get('solution') or {},
                    'task_id': str(create_data.get('taskId')) if create_data.get('taskId') else None,
                    'cost_usd': create_data.get('cost'),
                    'attempts': 0,
                }
            if create_status == 'failed':
                detail = (
                    create_data.get('errorDescription')
                    or create_data.get('errorCode')
                    or 'AnySolver createTask reported failed'
                )
                logger.warning('AnySolver %s createTask failed: %s payload=%s', purpose, detail, create_data)
                return {'status': 'failed', 'detail': str(detail)}

            task_id = create_data.get('taskId')
            if not task_id:
                logger.warning('AnySolver %s createTask missing taskId payload=%s', purpose, create_data)
                return {'status': 'failed', 'detail': 'AnySolver createTask returned no taskId'}
            task_id = str(task_id)

            for attempt in range(1, self.poll_attempts + 1):
                try:
                    poll_resp = await client.post(
                        f'{self.base_url}/getTaskResult',
                        json={'clientKey': self.api_key, 'taskId': task_id},
                    )
                    poll_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    if attempt == self.poll_attempts:
                        logger.warning(
                            'AnySolver %s getTaskResult HTTP error task_id=%s attempt=%s: %s',
                            purpose,
                            task_id,
                            attempt,
                            exc,
                        )
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
                    logger.warning(
                        'AnySolver %s task API error task_id=%s attempt=%s: %s payload=%s',
                        purpose,
                        task_id,
                        attempt,
                        detail,
                        poll_data,
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
                    logger.warning(
                        'AnySolver %s task failed task_id=%s attempt=%s: %s payload=%s',
                        purpose,
                        task_id,
                        attempt,
                        detail,
                        poll_data,
                    )
                    return {'status': 'failed', 'detail': str(detail), 'task_id': task_id, 'attempts': attempt}
                if status == 'ready':
                    return {
                        'status': 'ready',
                        'solution': poll_data.get('solution') or {},
                        'task_id': task_id,
                        'cost_usd': poll_data.get('cost'),
                        'attempts': attempt,
                    }
                logger.warning(
                    'AnySolver %s task unexpected status task_id=%s attempt=%s status=%r payload=%s',
                    purpose,
                    task_id,
                    attempt,
                    status,
                    poll_data,
                )
                return {
                    'status': 'failed',
                    'detail': f'AnySolver returned unexpected status: {status!r}',
                    'task_id': task_id,
                    'attempts': attempt,
                }

            logger.warning(
                'AnySolver %s task polling timed out task_id=%s attempts=%s',
                purpose,
                task_id,
                self.poll_attempts,
            )
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
        anysolver_session_id: str | None = None,
        attempts: int | None = None,
    ) -> dict:
        if challenge_row is not None and db is not None:
            challenge_row.solver_status = 'failed'
            challenge_row.error = detail[:MAX_ERROR_LENGTH]
            if task_id is not None:
                challenge_row.task_id = task_id
            if anysolver_session_id is not None:
                challenge_row.anysolver_session_id = anysolver_session_id
            if attempts is not None:
                challenge_row.attempts = attempts
            challenge_row.completed_at = datetime.now(timezone.utc)
            db.commit()
        logger.warning(
            'AnySolver captcha solve failed task_id=%s session_id=%s attempts=%s detail=%s',
            task_id,
            anysolver_session_id,
            attempts,
            detail,
        )
        return {'status': 'failed', 'detail': detail}
