from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelBase(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    enabled: bool = True
    output_template: str | None = Field(default=None, max_length=500)
    stream_password_enc: str | None = Field(default=None, max_length=255)
    preferred_quality: str = Field(default="best", max_length=30)


class ChannelCreate(ChannelBase):
    pass


class ChannelUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    enabled: bool = True
    output_template: str | None = Field(default=None, max_length=500)
    stream_password_enc: str | None = Field(default=None, max_length=255)
    preferred_quality: str = Field(default="best", max_length=30)


class ChannelRead(BaseModel):
    id: int
    user_id: str
    display_name: str | None
    enabled: bool
    output_template: str | None
    stream_password_enc: str | None
    preferred_quality: str
    last_status: str
    last_broad_no: int | None
    last_probe_at: str | None
    last_error: str | None
    offline_streak: int
    updated_at: str
