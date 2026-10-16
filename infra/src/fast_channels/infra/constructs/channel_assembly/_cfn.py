"""``core.specs`` shapes as CloudFormation properties, when their inputs hold CDK tokens."""

import re
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

_TOKEN: Final = re.compile(r"\$\{Token\[[^\]]*\]\}")


def with_tokens[R](build: Callable[..., R], **kwargs: Any) -> R:
    """``build(**kwargs)`` for a ``core.specs`` builder whose string arguments contain CDK tokens.

    The builders parse URLs and validate ADS variables, and an unresolved token (``${Token[...]}``) is neither a
    host nor free of brackets. Each token is swapped for a unique placeholder host during the call and put back in
    the result.
    """
    placeholders: dict[str, str] = {}

    def hide(match: re.Match[str]) -> str:
        placeholder = f"cdk-token-{len(placeholders)}.invalid"
        placeholders[placeholder] = match.group()
        return placeholder

    def restore(value: Any) -> Any:
        match value:
            case str():
                for placeholder, token in placeholders.items():
                    value = value.replace(placeholder, token)
                return value
            case dict():
                return {k: restore(v) for k, v in value.items()}
            case list():
                return [restore(v) for v in value]
            case _:
                return value

    return cast("R", restore(build(**{k: _TOKEN.sub(hide, v) if isinstance(v, str) else v for k, v in kwargs.items()})))


def override(resource: Any, properties: Mapping[str, Any]) -> None:
    """Set CloudFormation ``properties`` verbatim (``core.specs`` shapes are in CloudFormation case)."""
    for key, value in properties.items():
        resource.add_property_override(key, value)
