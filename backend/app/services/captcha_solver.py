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
# Cap exponential growth so polling delays remain bounded and predictable.
BACKOFF_MAX_EXPONENT = 4


class CaptchaSolverService:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.anysolver_api_key
        self.base_url = settings.anysolver_base_url.rstrip('/')
        self.poll_attempts = 20
        self.poll_base_delay_seconds = 2.0
        self.poll_max_delay_seconds = 8.0

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def is_captcha_challenge(payload: dict | None) -> bool:
        if not isinstance(payload, dict):
            return False
        return bool(payload.get('captcha_sitekey') and payload.get('captcha_rqdata'))

    async def solve_discord_challenge(
        self,
        challenge_payload: dict,
        *,
        token_id: int | None = None,
        guild_id: str | None = None,
        user_agent: str,
        db: Session | None = None,
    ) -> dict:
        if not self.is_enabled:
            return {'status': 'failed', 'detail': 'AnySolver API key is not configured'}
        if not self.is_captcha_challenge(challenge_payload):
            return {'status': 'failed', 'detail': 'Discord response does not contain a captcha challenge'}

        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = str(challenge_payload.get('captcha_rqdata') or '')
        session_id = challenge_payload.get('captcha_session_id')
        existing_rqtoken = challenge_payload.get('captcha_rqtoken')

        challenge_row: CaptchaChallenge | None = None
        if db is not None:
            challenge_row = CaptchaChallenge(
                token_id=token_id,
                guild_id=guild_id,
                challenge_type=str(challenge_payload.get('captcha_service') or 'hcaptcha'),
                sitekey=sitekey,
                rqdata=rqdata,
                solver_status='processing',
            )
            db.add(challenge_row)
            db.commit()
            db.refresh(challenge_row)

        create_payload = {
            'clientKey': self.api_key,
            'task': {
                'type': 'PopularCaptchaTokenProxyLess',
                'websiteURL': 'https://discord.com',
                'websiteKey': sitekey,
                'userAgent': user_agent,
                'rqdata': rqdata,
            },
        }
        if session_id:
            create_payload['task']['sessionId'] = session_id

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                create_resp = await client.post(f'{self.base_url}/createTask', json=create_payload)
                create_resp.raise_for_status()
            except httpx.HTTPError as exc:
                return await self._mark_failed(db, challenge_row, f'AnySolver createTask failed: {exc}')

            create_data = self._safe_json(create_resp)
            if create_data.get('errorId') not in (None, 0):
                detail = create_data.get('errorDescription') or create_data.get('errorCode') or 'AnySolver createTask failed'
                return await self._mark_failed(db, challenge_row, str(detail))

            task_id = create_data.get('taskId')
            if not task_id:
                return await self._mark_failed(db, challenge_row, 'AnySolver createTask returned no taskId')
            task_id = str(task_id)
            if challenge_row is not None:
                challenge_row.task_id = task_id
                challenge_row.attempts = 1
                db.commit()
                db.refresh(challenge_row)

            for attempt in range(1, self.poll_attempts + 1):
                try:
                    poll_resp = await client.post(
                        f'{self.base_url}/getTaskResult',
                        json={'clientKey': self.api_key, 'taskId': task_id},
                    )
                    poll_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    if attempt == self.poll_attempts:
                        return await self._mark_failed(db, challenge_row, f'AnySolver getTaskResult failed: {exc}')
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue

                poll_data = self._safe_json(poll_resp)
                if poll_data.get('errorId') not in (None, 0):
                    detail = poll_data.get('errorDescription') or poll_data.get('errorCode') or 'AnySolver task failed'
                    return await self._mark_failed(db, challenge_row, str(detail), task_id=task_id, attempts=attempt)

                status = poll_data.get('status')
                if status == 'processing':
                    if challenge_row is not None:
                        challenge_row.attempts = attempt
                        db.commit()
                    await asyncio.sleep(self._backoff_seconds(attempt))
                    continue
                if status == 'failed':
                    detail = poll_data.get('errorDescription') or poll_data.get('errorCode') or 'AnySolver reported failed status'
                    return await self._mark_failed(db, challenge_row, str(detail), task_id=task_id, attempts=attempt)
                if status == 'ready':
                    solution = poll_data.get('solution') or {}
                    token = solution.get('token')
                    rqtoken = solution.get('rqtoken') or existing_rqtoken
                    if not token:
                        return await self._mark_failed(db, challenge_row, 'AnySolver ready response missing solution token', task_id=task_id, attempts=attempt)
                    cost = poll_data.get('cost')
                    if challenge_row is not None:
                        challenge_row.task_id = task_id
                        challenge_row.solver_status = 'ready'
                        challenge_row.solved_token = token
                        challenge_row.cost_usd = str(cost) if cost is not None else None
                        challenge_row.attempts = attempt
                        challenge_row.completed_at = datetime.now(timezone.utc)
                        db.commit()
                    return {
                        'status': 'ready',
                        'captcha_key': token,
                        'captcha_rqtoken': rqtoken,
                        'task_id': task_id,
                        'cost_usd': cost,
                        'attempts': attempt,
                    }

            return await self._mark_failed(db, challenge_row, 'AnySolver task polling timed out', task_id=task_id, attempts=self.poll_attempts)

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        return {}

    def _backoff_seconds(self, attempt: int) -> float:
        exponent = min(max(0, attempt - 1), BACKOFF_MAX_EXPONENT)
        return min(self.poll_max_delay_seconds, self.poll_base_delay_seconds * (2 ** exponent))

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
        logger.warning('Captcha solve failed: %s', detail)
        return {'status': 'failed', 'detail': detail}
