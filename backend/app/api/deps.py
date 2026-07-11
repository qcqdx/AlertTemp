from typing import TYPE_CHECKING

from fastapi import Request

from app.ingest.service import IngestService

if TYPE_CHECKING:
    from app.rules.engine import RuleEngine


def get_ingest_service(request: Request) -> IngestService | None:
    return getattr(request.app.state, "ingest_service", None)


def get_rule_engine(request: Request) -> "RuleEngine | None":
    return getattr(request.app.state, "rule_engine", None)
