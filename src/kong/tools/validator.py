from typing import Any

from pydantic import ValidationError

from kong.tools.base import Tool
from kong.runtime.metrics import attributed


class ToolValidationError(ValueError):
    pass


@attributed("validation_seconds")
def validate_arguments(
    tool: Tool,
    arguments: dict[str, Any],
) -> dict[str, Any]:

    try:
        validated = tool.args_model.model_validate(arguments)

    except ValidationError as exc:
        raise ToolValidationError(str(exc)) from exc

    return validated.model_dump()
