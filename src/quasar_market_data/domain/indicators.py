"""Supported technical indicators and the ``indicators`` query grammar (pure, no I/O)."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

MAX_INDICATORS: Final = 10

_INTEGER: Final = re.compile(r"[0-9]{1,16}")


class IndicatorSyntaxError(ValueError):
    """The ``indicators`` text is malformed or names an unsupported indicator or parameter."""


@dataclass(frozen=True)
class ParamRange:
    name: str
    minimum: int
    maximum: int


@dataclass(frozen=True)
class IndicatorDefinition:
    name: str
    params: tuple[ParamRange, ...]
    outputs: tuple[str, ...]
    warmup: Callable[[Mapping[str, int]], int]


DEFINITIONS: Final[Mapping[str, IndicatorDefinition]] = {
    "rsi": IndicatorDefinition(
        name="rsi",
        params=(ParamRange("period", 2, 100),),
        outputs=("rsi",),
        warmup=lambda params: 10 * params["period"],
    ),
}


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    params: tuple[tuple[str, int], ...]

    @property
    def id(self) -> str:
        return f"{self.name}:" + ",".join(f"{key}={value}" for key, value in self.params)

    @property
    def outputs(self) -> tuple[str, ...]:
        return DEFINITIONS[self.name].outputs

    @property
    def warmup_slots(self) -> int:
        return DEFINITIONS[self.name].warmup(dict(self.params))

    def param(self, name: str) -> int:
        for key, value in self.params:
            if key == name:
                return value
        raise KeyError(name)


def _parse_item(item: str) -> IndicatorSpec:
    name, separator, param_text = item.partition(":")
    if not separator:
        raise IndicatorSyntaxError(f"indicator {item!r} must have the form name:param=value")
    definition = DEFINITIONS.get(name)
    if definition is None:
        raise IndicatorSyntaxError(f"unsupported indicator {name!r} in {item!r}; expected one of {sorted(DEFINITIONS)}")
    if not param_text:
        raise IndicatorSyntaxError(f"indicator {item!r} has no parameters")
    allowed = {p.name: p for p in definition.params}
    values: dict[str, int] = {}
    for pair in param_text.split(","):
        key, equals, value_text = pair.partition("=")
        if not equals or key not in allowed:
            raise IndicatorSyntaxError(
                f"invalid parameter {pair!r} in {item!r}; expected one of {sorted(allowed)} as name=value"
            )
        if key in values:
            raise IndicatorSyntaxError(f"duplicate parameter {key!r} in {item!r}")
        if _INTEGER.fullmatch(value_text) is None:
            raise IndicatorSyntaxError(f"parameter {key!r} in {item!r} must be a non-negative decimal integer")
        value = int(value_text)
        bounds = allowed[key]
        if not bounds.minimum <= value <= bounds.maximum:
            raise IndicatorSyntaxError(
                f"parameter {key!r} in {item!r} must be between {bounds.minimum} and {bounds.maximum}"
            )
        values[key] = value
    missing = [p.name for p in definition.params if p.name not in values]
    if missing:
        raise IndicatorSyntaxError(f"indicator {item!r} is missing parameters {missing}")
    return IndicatorSpec(name=name, params=tuple((p.name, values[p.name]) for p in definition.params))


def parse_indicators(text: str) -> tuple[IndicatorSpec, ...]:
    """Parse ``name:param=value[,param=value][;...]`` into specs in request order."""
    items = text.split(";")
    if len(items) > MAX_INDICATORS:
        raise IndicatorSyntaxError(f"at most {MAX_INDICATORS} indicators are allowed, got {len(items)}")
    specs = tuple(_parse_item(item) for item in items)
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise IndicatorSyntaxError(f"duplicate indicator {spec.id!r}")
        seen.add(spec.id)
    return specs
