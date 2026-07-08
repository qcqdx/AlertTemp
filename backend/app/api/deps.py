from fastapi import Request

from app.ingest.service import IngestService


def get_ingest_service(request: Request) -> IngestService | None:
    return getattr(request.app.state, "ingest_service", None)
