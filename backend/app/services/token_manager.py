from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models.research import AccountToken

logger = logging.getLogger('discord_research.token_manager')
RETRY_BASE_DELAY_SECONDS = 0.5
MIN_TOKEN_LENGTH = 20  # Shortest plausible Discord user token length


class TokenManagerService:
    @staticmethod
    def build_proxy_url(host: str, port: int, username: str, password: str) -> str:
        scheme = 'http'
        normalized_host = host
        if '://' in normalized_host:
            scheme, normalized_host = normalized_host.split('://', 1)
        if username and password:
            return f'{scheme}://{username}:{password}@{normalized_host}:{port}'
        return f'{scheme}://{normalized_host}:{port}'

    @staticmethod
    def token_hash(token_value: str) -> str:
        return hashlib.sha256(token_value.encode('utf-8')).hexdigest()

    @staticmethod
    def token_preview(token_value: str) -> str:
        if len(token_value) < 10:
            return '***'
        return f'{token_value[:4]}...{token_value[-4:]}'

    @staticmethod
    def normalize_token_value(raw_token_value: str) -> tuple[str, str | None]:
        value = raw_token_value.strip()
        if not value:
            raise ValueError('Token value cannot be empty')
        if value.lower().startswith('bot '):
            raise ValueError('User token must not include "Bot " prefix')
        if ':' in value:
            identity, separator, remainder = value.partition(':')
            if separator and '@' in identity:
                user_part, _, domain_part = identity.partition('@')
                if identity.count('@') != 1 or not user_part or not domain_part:
                    raise ValueError('Email portion of email:password:token format is invalid')
                if ':' not in remainder:
                    raise ValueError('Token is missing from email:password:token input')
                _, extracted = remainder.rsplit(':', 1)
                extracted = extracted.strip()
                if not extracted:
                    raise ValueError('Token is missing from email:password:token input')
                TokenManagerService._validate_token_format(extracted)
                return extracted, identity.strip()
            # No '@' in the first segment — treat the entire value as a plain token.
            # This handles tokens that legitimately contain ':' characters.

        TokenManagerService._validate_token_format(value)
        return value, None

    @staticmethod
    def _validate_token_format(token_value: str) -> None:
        if len(token_value) < MIN_TOKEN_LENGTH:
            raise ValueError('Token value is too short to be a valid Discord user token')
        if not token_value.startswith('mfa.') and token_value.count('.') < 2:
            raise ValueError('Token value format looks invalid for Discord user tokens')

    @staticmethod
    def parse_proxy(proxy_value: str | None) -> dict | None:
        if proxy_value is None:
            return None
        value = proxy_value.strip()
        if not value:
            return None

        scheme = 'http'
        raw_value = value
        if '://' in value:
            scheme_part, remainder = value.split('://', 1)
            if not scheme_part:
                raise ValueError('Proxy scheme cannot be empty')
            scheme = scheme_part.strip().lower()
            raw_value = remainder

        parts = raw_value.split(':', 3)
        if len(parts) not in (2, 4):
            raise ValueError('Proxy format must be host:port or host:port:username:password')

        host = parts[0].strip()
        port_text = parts[1].strip()
        username = parts[2].strip() if len(parts) > 2 else ''
        password = parts[3].strip() if len(parts) > 3 else ''
        if not host:
            raise ValueError('Proxy host cannot be empty')
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError('Proxy port must be numeric') from exc
        if port < 1 or port > 65535:
            raise ValueError('Proxy port must be between 1 and 65535')

        stored_host = f'{scheme}://{host}' if scheme != 'http' else host
        return {
            'host': stored_host,
            'port': port,
            'username': username,
            'password': password,
            'url': TokenManagerService.build_proxy_url(stored_host, port, username, password),
        }

    @staticmethod
    def proxy_preview(token: AccountToken) -> str | None:
        if not token.proxy_host or not token.proxy_port or not token.proxy_username or not token.proxy_password:
            return None
        return f'{token.proxy_host}:{token.proxy_port}:{token.proxy_username}:***'

    def upsert_token(
        self,
        db: Session,
        label: str,
        raw_token_value: str,
        rotation_priority: int,
        proxy_value: str | None = None,
    ) -> AccountToken:
        token_value, source_identity = self.normalize_token_value(raw_token_value)
        parsed_proxy = self.parse_proxy(proxy_value)
        token_hash = self.token_hash(token_value)
        record = db.query(AccountToken).filter(AccountToken.token_hash == token_hash).first()
        if record is None:
            record = AccountToken(
                label=label,
                token_value=token_value,
                source_identity=source_identity,
                token_hash=token_hash,
                proxy_host=parsed_proxy['host'] if parsed_proxy else None,
                proxy_port=parsed_proxy['port'] if parsed_proxy else None,
                proxy_username=parsed_proxy['username'] if parsed_proxy else None,
                proxy_password=parsed_proxy['password'] if parsed_proxy else None,
                rotation_priority=rotation_priority,
                health_status='unknown',
            )
            db.add(record)
        else:
            record.label = label
            record.token_value = token_value
            record.source_identity = source_identity
            record.proxy_host = parsed_proxy['host'] if parsed_proxy else None
            record.proxy_port = parsed_proxy['port'] if parsed_proxy else None
            record.proxy_username = parsed_proxy['username'] if parsed_proxy else None
            record.proxy_password = parsed_proxy['password'] if parsed_proxy else None
            record.rotation_priority = rotation_priority
            record.is_active = True
        db.commit()
        db.refresh(record)
        return record

    async def health_check(self, db: Session, token: AccountToken) -> AccountToken:
        headers = {'Authorization': token.token_value}
        proxy_url = None
        if token.proxy_host and token.proxy_port and token.proxy_username and token.proxy_password:
            proxy_url = self.build_proxy_url(
                host=token.proxy_host,
                port=token.proxy_port,
                username=token.proxy_username,
                password=token.proxy_password,
            )
        token.health_status = 'unknown'
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=15, proxy=proxy_url) as client:
                    response = await client.get('https://discord.com/api/v10/users/@me', headers=headers)
                if response.status_code == 200:
                    payload = response.json()
                    username = payload.get('global_name') or payload.get('username')
                    if username:
                        token.source_identity = username
                    token.health_status = 'healthy'
                    break
                if response.status_code == 401:
                    token.health_status = 'invalid'
                    break
                if response.status_code == 429 and attempt < max_attempts:
                    await self._sleep_before_retry(attempt)
                    continue
                if response.status_code >= 500 and attempt < max_attempts:
                    await self._sleep_before_retry(attempt)
                    continue
                token.health_status = 'invalid'
                logger.warning(
                    'health_check non-success token_id=%s status=%s body=%s',
                    token.id,
                    response.status_code,
                    response.text[:200],
                )
                break
            except httpx.HTTPError as exc:
                if attempt < max_attempts:
                    await self._sleep_before_retry(attempt)
                    continue
                token.health_status = 'unreachable'
                logger.warning('health_check request failed token_id=%s error=%s', token.id, exc)

        token.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(token)
        return token

    @staticmethod
    def mark_unhealthy(
        db: Session,
        token: AccountToken,
        *,
        deactivate: bool = True,
        status: str = 'invalid',
    ) -> AccountToken:
        token.health_status = status
        if deactivate:
            token.is_active = False
        token.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(token)
        return token

    @staticmethod
    async def _sleep_before_retry(attempt: int) -> None:
        base = min(2.0, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
        await asyncio.sleep(base + random.uniform(0.0, 0.2))

    def pick_for_rotation(self, db: Session) -> AccountToken | None:
        records = (
            db.query(AccountToken)
            .filter(AccountToken.is_active.is_(True))
            .order_by(AccountToken.health_status.desc(), AccountToken.usage_count.asc(), AccountToken.rotation_priority.asc())
            .all()
        )
        if not records:
            return None

        preferred = [item for item in records if item.health_status in {'healthy', 'unknown'}]
        selected = preferred[0] if preferred else records[0]
        selected.usage_count += 1
        db.commit()
        db.refresh(selected)
        return selected
