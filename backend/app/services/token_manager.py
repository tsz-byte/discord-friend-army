from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models.research import AccountToken


class TokenManagerService:
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
        parts = value.split(':')
        if len(parts) >= 3:
            if '@' not in parts[0]:
                raise ValueError('Email portion of email:password:token format must contain @ symbol')
            extracted = parts[-1].strip()
            if not extracted:
                raise ValueError('Token is missing from email:password:token input')
            return extracted, parts[0].strip()
        return value, None

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

        parts = raw_value.split(':')
        if len(parts) != 4:
            raise ValueError('Proxy format must be host:port:username:password')

        host, port_text, username, password = (part.strip() for part in parts)
        if not host or not username or not password:
            raise ValueError('Proxy format must include host, username, and password')
        if not port_text.isdigit():
            raise ValueError('Proxy port must be numeric')
        port = int(port_text)
        if port < 1 or port > 65535:
            raise ValueError('Proxy port must be between 1 and 65535')

        stored_host = f'{scheme}://{host}' if scheme != 'http' else host
        return {
            'host': stored_host,
            'port': port,
            'username': username,
            'password': password,
            'url': f'{scheme}://{username}:{password}@{host}:{port}',
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
            scheme = 'http'
            host = token.proxy_host
            if '://' in host:
                scheme, host = host.split('://', 1)
            proxy_url = f'{scheme}://{token.proxy_username}:{token.proxy_password}@{host}:{token.proxy_port}'
        try:
            async with httpx.AsyncClient(timeout=15, proxy=proxy_url) as client:
                response = await client.get('https://discord.com/api/v10/users/@me', headers=headers)
            token.health_status = 'healthy' if response.status_code == 200 else 'invalid'
        except httpx.HTTPError:
            token.health_status = 'unreachable'

        token.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(token)
        return token

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
