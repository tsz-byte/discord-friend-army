from __future__ import annotations

import os
import logging

from sqlalchemy.orm import Session

from app.models.research import AccountToken, ProxyEntry
from app.services.token_manager import TokenManagerService

logger = logging.getLogger('discord_research.file_loader')


class FileLoaderService:
    def __init__(self) -> None:
        self.token_manager = TokenManagerService()

    def load_tokens_file(self, db: Session, file_path: str) -> tuple[int, list[str]]:
        if not os.path.isfile(file_path):
            return 0, [f'Token file not found: {file_path}']
        loaded = 0
        errors: list[str] = []
        with open(file_path, 'r', encoding='utf-8') as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                try:
                    self.token_manager.upsert_token(
                        db=db,
                        label=f'token-{line_no}',
                        raw_token_value=line,
                        rotation_priority=line_no * 10,
                    )
                    loaded += 1
                except (ValueError, Exception) as exc:
                    errors.append(f'Line {line_no}: {exc}')
        return loaded, errors

    def load_proxies_file(self, db: Session, file_path: str) -> tuple[int, list[str]]:
        if not os.path.isfile(file_path):
            return 0, [f'Proxy file not found: {file_path}']
        logger.info('Loading proxies from file: %s', file_path)
        loaded = 0
        errors: list[str] = []
        with open(file_path, 'r', encoding='utf-8') as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                try:
                    parsed = self.token_manager.parse_proxy(line)
                    if parsed is None:
                        errors.append(f'Line {line_no}: empty proxy value')
                        continue
                    existing = (
                        db.query(ProxyEntry)
                        .filter(
                            ProxyEntry.host == parsed['host'],
                            ProxyEntry.port == parsed['port'],
                            ProxyEntry.username == parsed['username'],
                        )
                        .first()
                    )
                    if existing is None:
                        entry = ProxyEntry(
                            host=parsed['host'],
                            port=parsed['port'],
                            username=parsed['username'],
                            password=parsed['password'],
                        )
                        db.add(entry)
                    else:
                        existing.password = parsed['password']
                        existing.is_healthy = True
                    db.commit()
                    loaded += 1
                except (ValueError, Exception) as exc:
                    errors.append(f'Line {line_no}: {exc}')
                    logger.warning('Proxy line failed line=%s error=%s', line_no, exc)

        assigned = self._associate_loaded_proxies_to_tokens(db)
        logger.info('Proxy load finished: loaded=%s errors=%s associated_tokens=%s', loaded, len(errors), assigned)
        return loaded, errors

    def _associate_loaded_proxies_to_tokens(self, db: Session) -> int:
        proxies = db.query(ProxyEntry).order_by(ProxyEntry.id.asc()).all()
        tokens = db.query(AccountToken).order_by(AccountToken.id.asc()).all()
        if not proxies or not tokens:
            return 0

        changed = 0
        for index, token in enumerate(tokens):
            # Keep assignment deterministic and balanced: token[i] gets proxy[i % len(proxies)].
            proxy = proxies[index % len(proxies)]
            if (
                token.proxy_host == proxy.host
                and token.proxy_port == proxy.port
                and token.proxy_username == proxy.username
                and token.proxy_password == proxy.password
            ):
                continue
            token.proxy_host = proxy.host
            token.proxy_port = proxy.port
            token.proxy_username = proxy.username
            token.proxy_password = proxy.password
            changed += 1
        if changed:
            db.commit()
        return changed

    @staticmethod
    def load_api_config(file_path: str) -> dict:
        config: dict[str, str] = {}
        if not os.path.isfile(file_path):
            return config
        with open(file_path, 'r', encoding='utf-8') as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, _, value = line.partition('=')
                    config[key.strip()] = value.strip()
        return config
