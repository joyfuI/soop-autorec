from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings

ENCRYPTED_PREFIX = "enc:v1:"


def is_encrypted_value(value: str | None) -> bool:
    return bool(value) and value.startswith(ENCRYPTED_PREFIX)


def encrypt_password_value(settings: Settings, plaintext: str) -> str:
    if is_encrypted_value(plaintext):
        raise RuntimeError("password 입력값은 암호화 토큰이 아닌 평문이어야 합니다.")

    fernet = _build_fernet(settings)
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_password_value(settings: Settings, value: str | None) -> str | None:
    if value is None:
        return None
    if not is_encrypted_value(value):
        raise RuntimeError(
            "저장된 password 값이 암호화 형식이 아닙니다. 삭제 후 다시 저장해주세요."
        )

    token = value[len(ENCRYPTED_PREFIX) :]
    fernet = _build_fernet(settings)
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "저장된 password 복호화에 실패했습니다. APP_SECRET_KEY를 확인해주세요."
        ) from exc


def _build_fernet(settings: Settings) -> Fernet:
    key = _derive_fernet_key_from_app_secret(settings)
    if key is None:
        raise RuntimeError("password 저장 전에 APP_SECRET_KEY를 설정해주세요.")

    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - cryptography validates internals
        raise RuntimeError("APP_SECRET_KEY 값이 유효하지 않습니다.") from exc


def _derive_fernet_key_from_app_secret(settings: Settings) -> str | None:
    app_secret = (settings.app_secret_key or "").strip()
    if not app_secret:
        return None

    digest = hashlib.sha256(app_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")
