from urllib.parse import quote

SOOP_PLAYBACK_URL_TEMPLATE = "https://play.sooplive.co.kr/{userId}"


def build_playback_url(user_id: str) -> str:
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise ValueError("playback URL 생성을 위해 user_id가 필요합니다.")

    encoded_user_id = quote(normalized_user_id, safe="")
    return SOOP_PLAYBACK_URL_TEMPLATE.format(userId=encoded_user_id)
