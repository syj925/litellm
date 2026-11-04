from datetime import datetime
from pathlib import Path
from typing import Final

import psycopg
from pydantic import BaseModel, ConfigDict


class RequestOrganizationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    organization_id: str
    assignment_version: int
    organization_version: int
    membership_version: int
    captured_at: datetime


async def initialize_schema(connection: psycopg.AsyncConnection) -> None:
    schema: Final = "\n".join(
        Path(__file__).with_name(name).read_text(encoding="utf-8")
        for name in ("organization_schema.sql", "organization_attribution_schema.sql")
    )
    await connection.execute(schema, prepare=False)
