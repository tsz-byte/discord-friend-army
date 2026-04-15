import hashlib

from app.core.config import get_settings


class PrivacyService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def anonymize_user(self, guild_id: str, user_id: str) -> str:
        raw = f'{guild_id}:{user_id}:{self.settings.anonymization_salt}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def redact_content(content: str, limit: int = 180) -> str:
        safe = content.strip().replace('\n', ' ')
        return safe[:limit]
