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

    def upsert_token(self, db: Session, label: str, token_value: str, rotation_priority: int) -> AccountToken:
        token_hash = self.token_hash(token_value)
        record = db.query(AccountToken).filter(AccountToken.token_hash == token_hash).first()
        if record is None:
            record = AccountToken(
                label=label,
                token_value=token_value,
                token_hash=token_hash,
                rotation_priority=rotation_priority,
                health_status='unknown',
            )
            db.add(record)
        else:
            record.label = label
            record.token_value = token_value
            record.rotation_priority = rotation_priority
            record.is_active = True
        db.commit()
        db.refresh(record)
        return record

    async def health_check(self, db: Session, token: AccountToken) -> AccountToken:
        headers = {'Authorization': token.token_value}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
