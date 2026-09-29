from typing import Any

from pydantic import ValidationError

from kong.tools.base import Tool


class ToolValidationError(ValueError):
    pass


def validate_arguments(
    tool: Tool,
    arguments: dict[str, Any],
) -> dict[str, Any]:

    try:
        validated = tool.args_model.model_validate(arguments)

    except ValidationError as exc:
        raise ToolValidationError(str(exc)) from exc

    return validated.model_dump()