from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..tools import TOOL_REGISTRY, ToolCallResponse, ToolSpec


LabToolHandler = Callable[[dict[str, Any], dict[str, Any]], ToolCallResponse]
LAB_TOOL_HANDLERS: dict[str, LabToolHandler] = {}


def register_tool(
    spec: ToolSpec,
    *,
    lab_handler: LabToolHandler | None = None,
    replace: bool = False,
) -> None:
    TOOL_REGISTRY.register(spec, replace=replace)
    if lab_handler is not None:
        if spec.name in LAB_TOOL_HANDLERS and not replace:
            raise ValueError(f"lab tool handler already registered: {spec.name}")
        LAB_TOOL_HANDLERS[spec.name] = lab_handler


__all__ = ["LAB_TOOL_HANDLERS", "TOOL_REGISTRY", "register_tool"]
