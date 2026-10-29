"""How other modules add fields to the ``/policy`` patch without the PDS knowing them.

A :class:`PatchContributor` sees the request and the break's mode and returns a :class:`Contribution`: OpenRTB
``app.content`` fields and fields for the decision record. The content-enrichment module, for example, contributes the
show's metadata, or nothing for a holdout break. Contributions fail open: a contributor that raises, returns
something other than a :class:`Contribution` of JSON values, or sets a field another contributor already set, is
skipped and named in the result; the break is served without it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from fast_channels.core.pds.models import Mode, PolicyRequest

_FIELDS: Final = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class Contribution:
    """What one contributor adds to a break."""

    app_content: Mapping[str, JsonValue] = field(default_factory=dict)
    """Fields of OpenRTB ``app.content``."""
    record: Mapping[str, JsonValue] = field(default_factory=dict)
    """Fields of the policy decision record (for example the enrichment arm ``enr``)."""


class PatchContributor(Protocol):
    """A source of ``/policy`` patch fields."""

    @property
    def name(self) -> str:
        """Identifies the contributor in records and logs."""
        ...

    def contribute(self, request: PolicyRequest, mode: Mode) -> Contribution:
        """Fields for this break; an empty :class:`Contribution` for none."""
        ...


@dataclass(frozen=True, slots=True)
class Contributions:
    """Every contributor's fields, merged."""

    app_content: dict[str, JsonValue] = field(default_factory=dict)
    record: dict[str, JsonValue] = field(default_factory=dict)
    applied: tuple[str, ...] = ()
    """Contributors whose fields are in."""
    skipped: tuple[str, ...] = ()
    """Contributors that failed (raised, or returned anything but JSON fields) or collided with an earlier one."""


def collect(contributors: Iterable[PatchContributor], request: PolicyRequest, mode: Mode) -> Contributions:
    """Ask each contributor in turn and merge what they return (the first to set a field keeps it)."""
    app_content: dict[str, JsonValue] = {}
    record: dict[str, JsonValue] = {}
    applied: list[str] = []
    skipped: list[str] = []
    for contributor in contributors:
        try:
            contribution = contributor.contribute(request, mode)
            if not isinstance(contribution, Contribution):
                raise TypeError(f"{type(contribution).__name__} is not a Contribution")
            content = _FIELDS.validate_python(dict(contribution.app_content))
            fields = _FIELDS.validate_python(dict(contribution.record))
        except Exception:  # any failure serves the break without this contributor
            skipped.append(contributor.name)
            continue
        if app_content.keys() & content.keys() or record.keys() & fields.keys():
            skipped.append(contributor.name)
            continue
        app_content |= content
        record |= fields
        applied.append(contributor.name)
    return Contributions(app_content, record, tuple(applied), tuple(skipped))
