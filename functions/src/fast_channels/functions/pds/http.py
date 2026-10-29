"""What the PDS's load-balancer handlers share: JSON responses, and a refused request's field names."""

from typing import TYPE_CHECKING

from aws_lambda_powertools.event_handler import Response, content_types

if TYPE_CHECKING:
    from pydantic import ValidationError


def json_response(status: int, body: str) -> Response[str]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


def refused_fields(error: ValidationError) -> list[str]:
    """The locations of a validation error's problems, never their values (a value may be a ticket)."""
    return sorted({".".join(str(part) for part in problem["loc"]) for problem in error.errors()})
