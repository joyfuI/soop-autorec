import re

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def sanitize_filename_component(
    value: str,
    *,
    fallback: str = "untitled",
    max_len: int = 120,
) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value)
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" .")

    if not cleaned:
        cleaned = fallback

    return cleaned[:max_len]
