from datetime import datetime

from app.utils.sanitize import sanitize_filename_component
from app.utils.time import to_timezone


class FilenameRenderer:
    def __init__(self, timezone: str) -> None:
        self.timezone = timezone

    def render(
        self,
        template: str,
        *,
        display_name: str,
        user_id: str,
        title: str,
        broad_no: int,
        broad_start_at: datetime,
    ) -> str:
        local_dt = to_timezone(broad_start_at, self.timezone)
        safe_display_name = sanitize_filename_component(display_name or user_id)
        safe_title = sanitize_filename_component(title or "제목없음")

        replacements = {
            "${displayName}": safe_display_name,
            "${userId}": user_id,
            "${title}": safe_title,
            "${broadNo}": str(broad_no),
            "${YYMMDD}": local_dt.strftime("%y%m%d"),
            "${HHmmss}": local_dt.strftime("%H%M%S"),
        }

        output = template
        for key, value in replacements.items():
            output = output.replace(key, value)

        return output
