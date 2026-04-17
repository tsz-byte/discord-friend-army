from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.research import CaptchaChallenge

logger = logging.getLogger('discord_research.captcha_solver')
MAX_ERROR_LENGTH = 500
BACKOFF_MAX_EXPONENT = 4
SUPPORTED_CAPTCHA_SERVICES = ('anysolver', '2captcha', 'anticaptcha', 'deathbycaptcha')


class BaseCaptchaService(ABC):
    default_base_url: str

    def __init__(
        self,
        *,
        service_name: str,
        api_key: str,
        base_url: str,
        task_type: str,
        verify: bool | str,
        timeout_seconds: float,
    ) -> None:
        self.service_name = service_name
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.task_type = task_type
        self.verify = verify
        self.timeout_seconds = timeout_seconds

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key)

    @abstractmethod
    async def solve(
        self,
        challenge_payload: dict,
        *,
        user_agent: str,
        poll_attempts: int,
        backoff_callback,
    ) -> dict:
        raise NotImplementedError


class CreateTaskCaptchaService(BaseCaptchaService):
    async def solve(
        self,
        challenge_payload: dict,
        *,
        user_agent: str,
        poll_attempts: int,
        backoff_callback,
    ) -> dict:
        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = challenge_payload.get('captcha_rqdata')
        session_id = challenge_payload.get('captcha_session_id')
        existing_rqtoken = challenge_payload.get('captcha_rqtoken')
        website_url = str(challenge_payload.get('captcha_website_url') or 'https://discord.com')

        task_payload = {
            'type': self.task_type,
            'websiteURL': website_url,
            'websiteKey': sitekey,
            'userAgent': user_agent,
        }
        if rqdata:
            rqdata_value = str(rqdata)
            task_payload['rqdata'] = rqdata_value
            task_payload['data'] = rqdata_value
        if session_id:
            task_payload['sessionId'] = str(session_id)

        create_payload = {
            'clientKey': self.api_key,
            'task': task_payload,
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=self.verify) as client:
            try:
                create_resp = await client.post(f'{self.base_url}/createTask', json=create_payload)
                create_resp.raise_for_status()
            except httpx.HTTPError as exc:
                return {'status': 'failed', 'detail': f'{self.service_name} createTask failed: {exc}'}

            create_data = CaptchaSolverService._safe_json(create_resp)
            if create_data.get('errorId') not in (None, 0):
                detail = create_data.get('errorDescription') or create_data.get('errorCode') or f'{self.service_name} createTask failed'
                return {'status': 'failed', 'detail': str(detail)}

            task_id = create_data.get('taskId')
            if not task_id:
                return {'status': 'failed', 'detail': f'{self.service_name} createTask returned no taskId'}
            task_id = str(task_id)

            for attempt in range(1, poll_attempts + 1):
                try:
                    poll_resp = await client.post(
                        f'{self.base_url}/getTaskResult',
                        json={'clientKey': self.api_key, 'taskId': task_id},
                    )
                    poll_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    if attempt == poll_attempts:
                        return {'status': 'failed', 'detail': f'{self.service_name} getTaskResult failed: {exc}', 'task_id': task_id, 'attempts': attempt}
                    await asyncio.sleep(backoff_callback(attempt))
                    continue

                poll_data = CaptchaSolverService._safe_json(poll_resp)
                if poll_data.get('errorId') not in (None, 0):
                    detail = poll_data.get('errorDescription') or poll_data.get('errorCode') or f'{self.service_name} task failed'
                    return {'status': 'failed', 'detail': str(detail), 'task_id': task_id, 'attempts': attempt}

                status = poll_data.get('status')
                if status == 'processing':
                    await asyncio.sleep(backoff_callback(attempt))
                    continue
                if status == 'failed':
                    detail = poll_data.get('errorDescription') or poll_data.get('errorCode') or f'{self.service_name} reported failed status'
                    return {'status': 'failed', 'detail': str(detail), 'task_id': task_id, 'attempts': attempt}
                if status == 'ready':
                    solution = poll_data.get('solution') or {}
                    token = solution.get('token') or solution.get('gRecaptchaResponse') or solution.get('hcaptchaResponse')
                    rqtoken = solution.get('rqtoken') or existing_rqtoken
                    if not token:
                        return {
                            'status': 'failed',
                            'detail': f'{self.service_name} ready response missing solution token',
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

            return {'status': 'failed', 'detail': f'{self.service_name} task polling timed out', 'task_id': task_id, 'attempts': poll_attempts}


class AnySolverCaptchaService(CreateTaskCaptchaService):
    default_base_url = 'https://api.anysolver.com'


class TwoCaptchaService(CreateTaskCaptchaService):
    default_base_url = 'https://api.2captcha.com'


class AntiCaptchaService(CreateTaskCaptchaService):
    default_base_url = 'https://api.anti-captcha.com'


class DeathByCaptchaService(BaseCaptchaService):
    default_base_url = 'https://api.dbcapi.me'

    async def solve(
        self,
        challenge_payload: dict,
        *,
        user_agent: str,
        poll_attempts: int,
        backoff_callback,
    ) -> dict:
        if ':' not in self.api_key:
            return {
                'status': 'failed',
                'detail': 'deathbycaptcha requires DFA_DEATHBYCAPTCHA_API_KEY in username:password format',
            }
        username, password = self.api_key.split(':', 1)
        if not username or not password:
            return {
                'status': 'failed',
                'detail': 'deathbycaptcha requires non-empty username and password',
            }
        sitekey = str(challenge_payload.get('captcha_sitekey') or '')
        rqdata = challenge_payload.get('captcha_rqdata')
        existing_rqtoken = challenge_payload.get('captcha_rqtoken')
        website_url = str(challenge_payload.get('captcha_website_url') or 'https://discord.com')

        token_params = {'pageurl': website_url, 'sitekey': sitekey, 'userAgent': user_agent}
        if rqdata:
            token_params['rqdata'] = str(rqdata)

        create_data = {
            'username': username,
            'password': password,
            'type': self.task_type,
            'sitekey': sitekey,
            'pageurl': website_url,
            'token_params': json.dumps(token_params),
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=self.verify) as client:
            try:
                create_resp = await client.post(f'{self.base_url}/api/captcha', data=create_data)
                create_resp.raise_for_status()
            except httpx.HTTPError as exc:
                return {'status': 'failed', 'detail': f'deathbycaptcha create failed: {exc}'}

            create_payload = CaptchaSolverService._safe_json(create_resp)
            captcha_id = create_payload.get('captcha') or create_payload.get('id')
            if not captcha_id:
                raw_text = str(create_payload.get('raw_text') or '')
                first_segment = raw_text.split(',', 1)[0] if raw_text else ''
                if first_segment.isdigit():
                    captcha_id = first_segment
            if not captcha_id:
                return {'status': 'failed', 'detail': 'deathbycaptcha create returned no captcha id'}
            captcha_id = str(captcha_id)

            for attempt in range(1, poll_attempts + 1):
                try:
                    poll_resp = await client.get(
                        f'{self.base_url}/api/captcha/{captcha_id}',
                        params={'username': username, 'password': password},
                    )
                    poll_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    if attempt == poll_attempts:
                        return {'status': 'failed', 'detail': f'deathbycaptcha poll failed: {exc}', 'task_id': captcha_id, 'attempts': attempt}
                    await asyncio.sleep(backoff_callback(attempt))
                    continue

                poll_data = CaptchaSolverService._safe_json(poll_resp)
                token = poll_data.get('text') or poll_data.get('token') or poll_data.get('captcha')
                if not token:
                    raw_text = str(poll_data.get('raw_text') or '')
                    if ',' in raw_text:
                        token_candidate = raw_text.rsplit(',', 1)[-1]
                        if token_candidate and token_candidate not in ('0', 'CAPTCHA_NOT_READY', 'NOT_READY'):
                            token = token_candidate
                is_correct = poll_data.get('is_correct')
                if token and str(token) not in ('0', 'CAPTCHA_NOT_READY', 'NOT_READY') and is_correct not in (False, 0, '0'):
                    return {
                        'status': 'ready',
                        'captcha_key': str(token),
                        'captcha_rqtoken': existing_rqtoken,
                        'captcha_rqdata': str(rqdata) if rqdata is not None else None,
                        'task_id': captcha_id,
                        'attempts': attempt,
                    }
                await asyncio.sleep(backoff_callback(attempt))

            return {'status': 'failed', 'detail': 'deathbycaptcha polling timed out', 'task_id': captcha_id, 'attempts': poll_attempts}


class CaptchaSolverService:
    def __init__(self) -> None:
        settings = get_settings()
        self.poll_attempts = 20
        self.poll_base_delay_seconds = 2.0
        self.poll_max_delay_seconds = 8.0
        self.task_type = settings.captcha_task_type
        self.verify = self._build_verify_value(settings)

        self._service_names = self._resolve_service_order(settings)
        self._next_start_index = 0
        self._health: dict[str, dict[str, int | str]] = {}
        self._services = self._build_services(settings)

    @property
    def is_enabled(self) -> bool:
        return any(service.is_enabled for service in self._services.values())

    @staticmethod
    def is_captcha_challenge(payload: dict | None) -> bool:
        if not isinstance(payload, dict):
            return False
        # Discord occasionally omits rqdata for some challenge variants, so
        # sitekey-only challenges are still considered solvable.
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
        if not self.is_enabled:
            return {'status': 'failed', 'detail': 'No captcha solver API key is configured'}
        if not self.is_captcha_challenge(challenge_payload):
            return {'status': 'failed', 'detail': 'Discord response does not contain a captcha challenge'}

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

        attempted_services: list[str] = []
        failure_details: list[str] = []

        for service_name in self._rotated_service_names():
            service = self._services.get(service_name)
            if service is None or not service.is_enabled:
                continue
            attempted_services.append(service_name)
            logger.info('Captcha solve attempt service=%s token_id=%s guild_id=%s', service_name, token_id, guild_id)
            result = await service.solve(
                challenge_payload,
                user_agent=user_agent,
                poll_attempts=self.poll_attempts,
                backoff_callback=self._backoff_seconds,
            )
            if result.get('status') == 'ready':
                self._record_service_success(service_name)
                if challenge_row is not None:
                    challenge_row.task_id = result.get('task_id')
                    challenge_row.solver_status = 'ready'
                    challenge_row.solved_token = str(result.get('captcha_key') or '')
                    cost = result.get('cost_usd')
                    challenge_row.cost_usd = str(cost) if cost is not None else None
                    challenge_row.attempts = int(result.get('attempts') or 0)
                    challenge_row.completed_at = datetime.now(timezone.utc)
                    db.commit()
                logger.info('Captcha solved via service=%s after trying=%s', service_name, attempted_services)
                return {
                    'status': 'ready',
                    'captcha_key': result.get('captcha_key'),
                    'captcha_rqtoken': result.get('captcha_rqtoken'),
                    'captcha_rqdata': result.get('captcha_rqdata'),
                    'task_id': result.get('task_id'),
                    'cost_usd': result.get('cost_usd'),
                    'attempts': result.get('attempts'),
                    'service': service_name,
                }

            detail = str(result.get('detail') or f'{service_name} solve failed')
            failure_details.append(f'{service_name}: {detail}')
            self._record_service_failure(service_name, detail)
            logger.warning('Captcha solver service failed service=%s detail=%s next_service=%s', service_name, detail, self._peek_next_service(service_name))

        if not attempted_services:
            return await self._mark_failed(db, challenge_row, 'No enabled captcha solver services are configured')

        aggregated = '; '.join(failure_details)
        return await self._mark_failed(db, challenge_row, f'All captcha services failed. Attempts: {aggregated}')

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        text = ''
        try:
            text = response.text.strip()
        except Exception:
            text = ''
        if text:
            return {'raw_text': text}
        return {}

    def _backoff_seconds(self, attempt: int) -> float:
        exponent = min(max(0, attempt - 1), BACKOFF_MAX_EXPONENT)
        return min(self.poll_max_delay_seconds, self.poll_base_delay_seconds * (2 ** exponent))

    def _build_verify_value(self, settings) -> bool | str:
        if not settings.captcha_ssl_verify:
            if str(settings.app_env).lower() in ('prod', 'production'):
                logger.error('Captcha SSL certificate verification is disabled in production mode. Re-enable DFA_CAPTCHA_SSL_VERIFY as soon as possible.')
            logger.warning('Captcha SSL certificate verification is disabled. This is insecure and should only be used for troubleshooting.')
            return False
        if settings.captcha_ca_bundle_path:
            logger.info('Captcha solver using custom CA bundle: %s', settings.captcha_ca_bundle_path)
            return settings.captcha_ca_bundle_path
        return True

    def _resolve_service_order(self, settings) -> list[str]:
        configured_primary = [item.strip().lower() for item in str(settings.captcha_service).split(',') if item.strip()]
        configured_fallback = [item.strip().lower() for item in str(settings.captcha_fallback_services).split(',') if item.strip()]
        combined = configured_primary + configured_fallback
        order: list[str] = []
        for service_name in combined:
            if service_name in SUPPORTED_CAPTCHA_SERVICES and service_name not in order:
                order.append(service_name)
        if not order:
            order = ['anysolver']
        return order

    def _service_api_key(self, settings, service_name: str) -> str:
        common_api_key = settings.captcha_api_key
        if service_name == 'anysolver':
            return common_api_key or settings.anysolver_api_key
        if service_name == '2captcha':
            return settings.captcha_2captcha_api_key or common_api_key
        if service_name == 'anticaptcha':
            return settings.anticaptcha_api_key or common_api_key
        if service_name == 'deathbycaptcha':
            return settings.deathbycaptcha_api_key or common_api_key
        return ''

    def _service_base_url(self, settings, service_name: str, default_base_url: str) -> str:
        if settings.captcha_base_url:
            return settings.captcha_base_url
        if service_name == 'anysolver' and settings.anysolver_base_url:
            return settings.anysolver_base_url
        return default_base_url

    def _build_services(self, settings) -> dict[str, BaseCaptchaService]:
        service_classes: dict[str, type[BaseCaptchaService]] = {
            'anysolver': AnySolverCaptchaService,
            '2captcha': TwoCaptchaService,
            'anticaptcha': AntiCaptchaService,
            'deathbycaptcha': DeathByCaptchaService,
        }

        services: dict[str, BaseCaptchaService] = {}
        for service_name in self._service_names:
            service_cls = service_classes.get(service_name)
            if service_cls is None:
                continue
            services[service_name] = service_cls(
                service_name=service_name,
                api_key=self._service_api_key(settings, service_name),
                base_url=self._service_base_url(settings, service_name, service_cls.default_base_url),
                task_type=self.task_type,
                verify=self.verify,
                timeout_seconds=30,
            )
            self._health[service_name] = {'successes': 0, 'failures': 0, 'last_error': ''}
        return services

    def _rotated_service_names(self) -> list[str]:
        if not self._service_names:
            return []

        health_sorted = sorted(
            self._service_names,
            key=self._failure_count,
        )
        start_service = self._service_names[self._next_start_index % len(self._service_names)]
        if start_service in health_sorted:
            pivot = health_sorted.index(start_service)
            return health_sorted[pivot:] + health_sorted[:pivot]
        return health_sorted

    def _record_service_success(self, service_name: str) -> None:
        health = self._health.setdefault(service_name, {'successes': 0, 'failures': 0, 'last_error': ''})
        health['successes'] = int(health.get('successes', 0)) + 1
        health['last_error'] = ''
        if service_name in self._service_names:
            self._next_start_index = self._service_names.index(service_name)

    def _record_service_failure(self, service_name: str, detail: str) -> None:
        health = self._health.setdefault(service_name, {'successes': 0, 'failures': 0, 'last_error': ''})
        health['failures'] = int(health.get('failures', 0)) + 1
        health['last_error'] = detail[:MAX_ERROR_LENGTH]
        if service_name in self._service_names:
            failed_index = self._service_names.index(service_name)
            self._next_start_index = (failed_index + 1) % len(self._service_names)

    def _peek_next_service(self, current_service: str) -> str | None:
        if current_service not in self._service_names:
            return None
        idx = self._service_names.index(current_service)
        if len(self._service_names) <= 1:
            return None
        return self._service_names[(idx + 1) % len(self._service_names)]

    def _failure_count(self, service_name: str) -> int:
        failures = self._health.get(service_name, {}).get('failures', 0)
        return int(failures) if isinstance(failures, (int, str)) and str(failures).isdigit() else 0

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
