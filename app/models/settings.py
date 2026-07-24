from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.config import Settings
from app.db import connect
from app.services import secrets as secrets_service

SOOP_USERNAME_KEY = "soop_username"
SOOP_PASSWORD_KEY = "soop_password"
COOKIES_TXT_PATH_KEY = "cookies_txt_path"
CONTROL_PROXY_URL_KEY = "control_proxy_url"
_UNSET = object()


def list_settings(settings: Settings) -> dict[str, str]:
    with connect(settings) as conn:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key ASC").fetchall()

    return {row["key"]: row["value"] for row in rows}


def get_setting(settings: Settings, key: str, default: str | None = None) -> str | None:
    with connect(settings) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()

    if row is None:
        return default
    return str(row["value"])


def upsert_setting(settings: Settings, key: str, value: str) -> None:
    with connect(settings) as conn:
        _upsert_setting(conn, key, value)
        conn.commit()


def delete_setting(settings: Settings, key: str) -> None:
    with connect(settings) as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def _upsert_setting(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value = excluded.value
        """,
        (key, value),
    )


def _apply_optional_setting(conn, key: str, value: str | None | object) -> None:
    if value is _UNSET:
        return
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    _upsert_setting(conn, key, value)


def get_auth_settings(settings: Settings) -> dict[str, str | None | bool]:
    username = get_setting(settings, SOOP_USERNAME_KEY)
    stored_password = get_setting(settings, SOOP_PASSWORD_KEY)
    cookies_txt_path = get_setting(settings, COOKIES_TXT_PATH_KEY)

    return {
        "username": username,
        "has_password": bool(stored_password),
        "cookies_txt_path": cookies_txt_path,
    }


def get_auth_credentials(settings: Settings) -> dict[str, str | None]:
    username = get_setting(settings, SOOP_USERNAME_KEY)
    stored_password = get_setting(settings, SOOP_PASSWORD_KEY)
    cookies_txt_path = get_setting(settings, COOKIES_TXT_PATH_KEY)

    password: str | None = None
    if stored_password:
        password = secrets_service.decrypt_password_value(settings, stored_password)

    return {
        "username": username,
        "password": password,
        "cookies_txt_path": cookies_txt_path,
    }


def update_auth_settings(
    settings: Settings,
    *,
    username: str | None,
    password: str | None,
    cookies_txt_path: str | None,
    clear_password: bool = False,
) -> dict[str, str | None | bool]:
    username_value: str | None | object = _UNSET
    if username is not None:
        username_value = username.strip() or None

    password_value: str | None | object = _UNSET
    if clear_password:
        password_value = None
    elif password is not None:
        if password.strip():
            password_value = secrets_service.encrypt_password_value(settings, password)

    cookies_txt_path_value: str | None | object = _UNSET
    if cookies_txt_path is not None:
        cookies_txt_path_value = cookies_txt_path.strip() or None

    with connect(settings) as conn:
        _apply_optional_setting(conn, SOOP_USERNAME_KEY, username_value)
        _apply_optional_setting(conn, SOOP_PASSWORD_KEY, password_value)
        _apply_optional_setting(conn, COOKIES_TXT_PATH_KEY, cookies_txt_path_value)
        conn.commit()

    return get_auth_settings(settings)


def get_proxy_settings(settings: Settings) -> dict[str, str | None]:
    return {
        "proxy_url": get_setting(settings, CONTROL_PROXY_URL_KEY),
    }


def update_proxy_settings(
    settings: Settings,
    *,
    proxy_url: str | None,
) -> dict[str, str | None]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        delete_setting(settings, CONTROL_PROXY_URL_KEY)
        return get_proxy_settings(settings)

    normalized = normalize_proxy_url(normalized)
    upsert_setting(settings, CONTROL_PROXY_URL_KEY, normalized)
    return get_proxy_settings(settings)


def normalize_proxy_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    _validate_proxy_url_parts(parsed)

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy_url port가 올바르지 않습니다.") from exc

    if port is not None:
        hostname = f"{hostname}:{port}"

    netloc = f"{_normalize_proxy_userinfo(parsed.username, parsed.password)}{hostname}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _validate_proxy_url_parts(parsed) -> None:
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("proxy_url 스킴은 http, https, socks5, socks5h 중 하나여야 합니다.")
    if not parsed.hostname:
        raise ValueError("proxy_url에는 hostname이 포함되어야 합니다.")


def _normalize_proxy_userinfo(username: str | None, password: str | None) -> str:
    if username is None and password is None:
        return ""

    userinfo = ""
    if username is not None:
        userinfo = quote(unquote(username), safe="")

    if password is not None:
        userinfo = f"{userinfo}:{quote(unquote(password), safe='')}"

    return f"{userinfo}@"
