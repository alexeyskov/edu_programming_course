from __future__ import annotations

import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings


class CSRFProtection:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.serializer = URLSafeTimedSerializer(
            settings.secret_key.get_secret_value(), salt="eduprog-csrf-v1"
        )

    def issue(self) -> str:
        return self.serializer.dumps(secrets.token_urlsafe(32))

    def valid(self, token: str) -> bool:
        try:
            self.serializer.loads(token, max_age=self.settings.csrf_ttl_seconds)
        except (BadSignature, SignatureExpired):
            return False
        return True
