"""
Shared tool schema generation and execution helpers.
"""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Any, Dict, Type

from pydantic import BaseModel, ConfigDict, Field, create_model

from core.response_utils import attach_request_id
from core.tool_catalog import ToolSpec


class DynamicArgsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@lru_cache(maxsize=128)
def _build_dynamic_model(name: str, func: object) -> Type[BaseModel]:
    signature = inspect.signature(func)
    fields: Dict[str, tuple[Any, Any]] = {}
    for param_name, param in signature.parameters.items():
        annotation = param.annotation if param.annotation is not inspect._empty else Any
        default = param.default if param.default is not inspect._empty else ...
        fields[param_name] = (annotation, Field(default=default))
    return create_model(f"{name.title().replace('_', '')}Args", __base__=DynamicArgsModel, **fields)


def get_request_model(spec: ToolSpec) -> Type[BaseModel]:
    if spec.request_model is not None:
        return spec.request_model
    if spec.func is None:
        raise RuntimeError(f"Tool '{spec.name}' is not bound to a runtime function")
    return _build_dynamic_model(spec.name, spec.func)


def get_parameters_schema(spec: ToolSpec) -> Dict[str, Any]:
    schema = get_request_model(spec).model_json_schema()
    schema.pop("title", None)
    return schema


async def execute_tool(spec: ToolSpec, arguments: Dict[str, Any], request_id: str | None = None) -> Dict[str, Any]:
    if spec.func is None:
        raise RuntimeError(f"Tool '{spec.name}' is not bound to a runtime function")
    model = get_request_model(spec)
    payload = model.model_validate(arguments or {})
    result = await spec.func(**payload.model_dump(exclude_unset=True))
    return attach_request_id(result, request_id)
