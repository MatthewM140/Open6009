"""Hardware-independent V2 workspace identity and scope layout.

Views reference immutable signal IDs. Editing a view never changes acquisition
settings or sends a device command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .math_signals import MathProgram, compile_math

AI_PINS = (2, 5, 8, 11, 3, 6, 9, 12)
AO_PINS = (14, 15)


@dataclass
class Signal:
    id: str
    name: str = ""
    active: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name else self.id


@dataclass
class Scope:
    id: str
    name: str
    signals: list[str] = field(default_factory=list)
    collapsed: bool = False
    follow_live: bool = True
    kind: str = "analog"


@dataclass
class MathDefinition:
    id: str
    handle: str
    expression: str
    unit: str
    dependencies: tuple[str, ...]


class Workspace:
    def __init__(self) -> None:
        self.signals = {
            **{f"AI{i}": Signal(f"AI{i}") for i in range(8)},
            **{f"AO{i}": Signal(f"AO{i}") for i in range(2)},
            **{f"P0.{i}": Signal(f"P0.{i}") for i in range(8)},
            **{f"P1.{i}": Signal(f"P1.{i}") for i in range(4)},
            "CTR0": Signal("CTR0"),
        }
        self.scopes: list[Scope] = []
        self.math: dict[str, MathDefinition] = {}
        self._math_programs: dict[str, MathProgram] = {}

    def rename(self, signal_id: str, name: str) -> None:
        name = name.strip()
        if len(name) > 64 or any(ord(char) < 32 for char in name):
            raise ValueError("channel name must be at most 64 printable characters")
        self.signals[signal_id].name = name

    def references(self, signal_id: str) -> tuple[str, ...]:
        dependent = {signal_id}
        owners: list[str] = []
        changed = True
        while changed:
            changed = False
            for definition in self.math.values():
                if (definition.id not in dependent
                        and any(key in dependent for key in definition.dependencies)):
                    dependent.add(definition.id)
                    owners.append(self._friendly_label(definition.id))
                    changed = True
        owners.extend(scope.name for scope in self.scopes
                      if any(key in dependent for key in scope.signals))
        return tuple(dict.fromkeys(owners))

    def _friendly_label(self, signal_id: str) -> str:
        if signal_id in self.math:
            definition = self.math[signal_id]
            return f"{self.signals[signal_id].name or definition.handle} ({definition.handle})"
        return self.signals[signal_id].label

    def _check_math_sources_active(self, dependencies: tuple[str, ...]) -> None:
        for identifier in dependencies:
            if not self.signals[identifier].active:
                raise ValueError(f"math source {self._friendly_label(identifier)} is inactive")

    def set_active(self, signal_id: str, active: bool) -> None:
        if not active:
            users = self.references(signal_id)
            if users:
                raise ValueError(
                    f"Cannot deactivate {self.signals[signal_id].label}: used by "
                    + ", ".join(users) + ". Remove it from those scopes first."
                )
        self.signals[signal_id].active = active

    def scope(self, scope_id: str) -> Scope:
        return next(scope for scope in self.scopes if scope.id == scope_id)

    def add_scope(self, signals: tuple[str, ...] = (), *, name: str | None = None,
                  kind: str = "analog") -> Scope:
        if kind not in {"analog", "digital"}:
            raise ValueError("unsupported scope kind")
        if len(signals) > (12 if kind == "digital" else 8):
            raise ValueError("scope has too many signals")
        for signal_id in signals:
            if signal_id not in self.signals or not self.signals[signal_id].active:
                raise ValueError(f"{signal_id} is not active")
        if any(not self._plottable(signal_id, kind) for signal_id in signals):
            raise ValueError(f"scope does not accept these {kind} signals")
        number = 1
        prefix = "Scope" if kind == "analog" else "Digital Scope"
        while any(scope.name == f"{prefix} {number}" for scope in self.scopes):
            number += 1
        scope = Scope(f"scope:{uuid4().hex}", name or f"{prefix} {number}",
                      list(dict.fromkeys(signals)), kind=kind)
        self.scopes.append(scope)
        return scope

    def set_scope_signals(self, scope_id: str, signals: tuple[str, ...]) -> None:
        for signal_id in signals:
            if signal_id not in self.signals or not self.signals[signal_id].active:
                raise ValueError(f"{signal_id} is not active")
        kind = self.scope(scope_id).kind
        if len(signals) > (12 if kind == "digital" else 8):
            raise ValueError("scope has too many signals")
        if any(not self._plottable(signal_id, kind) for signal_id in signals):
            raise ValueError(f"scope does not accept these {kind} signals")
        self.scope(scope_id).signals = list(dict.fromkeys(signals))

    def _plottable(self, signal_id: str, kind: str = "analog") -> bool:
        if kind == "digital":
            return signal_id.startswith(("P0.", "P1."))
        return signal_id.startswith("AI") or (
            signal_id in self.math and self.math[signal_id].unit == "V")

    def _symbols(self) -> dict[str, tuple[str, str]]:
        symbols = {f"AI{channel}": (f"AI{channel}", "V") for channel in range(8)}
        symbols.update({item.handle: (item.id, item.unit) for item in self.math.values()})
        return symbols

    def _check_math_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(identifier: str) -> None:
            if identifier in visiting:
                raise ValueError("math dependency cycle")
            if identifier in visited:
                return
            visiting.add(identifier)
            for dependency in self.math[identifier].dependencies:
                if dependency in self.math:
                    walk(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in self.math:
            walk(identifier)

    def math_order(self) -> tuple[str, ...]:
        order: list[str] = []
        seen: set[str] = set()

        def walk(identifier: str) -> None:
            if identifier in seen:
                return
            for dependency in self.math[identifier].dependencies:
                if dependency in self.math:
                    walk(dependency)
            seen.add(identifier)
            order.append(identifier)

        for identifier in self.math:
            walk(identifier)
        return tuple(order)

    def add_math(self, name: str, expression: str) -> MathDefinition:
        clean_name = name.strip()
        if len(clean_name) > 64 or any(ord(char) < 32 for char in clean_name):
            raise ValueError("channel name must be at most 64 printable characters")
        number = 1
        while any(item.handle == f"M{number}" for item in self.math.values()):
            number += 1
        program = compile_math(expression, self._symbols())
        self._check_math_sources_active(program.dependencies)
        identifier = f"math:{uuid4().hex}"
        definition = MathDefinition(identifier, f"M{number}", expression,
                                    program.unit, program.dependencies)
        self.signals[identifier] = Signal(identifier, name=clean_name, active=True)
        self.math[identifier] = definition
        self._math_programs[identifier] = program
        return definition

    def edit_math(self, identifier: str, expression: str) -> None:
        old = self.math[identifier]
        program = compile_math(expression, self._symbols())
        self._check_math_sources_active(program.dependencies)
        if program.unit != old.unit:
            raise ValueError("editing a math signal cannot change its unit")
        self.math[identifier] = MathDefinition(identifier, old.handle, expression,
                                               program.unit, program.dependencies)
        try:
            self._check_math_cycles()
        except ValueError:
            self.math[identifier] = old
            raise
        self._math_programs[identifier] = program

    def delete_math(self, identifier: str) -> None:
        users = self.references(identifier)
        if users:
            raise ValueError(f"Cannot delete {self.signals[identifier].label}: used by "
                             + ", ".join(users))
        del self.math[identifier]
        del self.signals[identifier]
        del self._math_programs[identifier]

    def transfer(self, source_id: str, target_id: str, signals: tuple[str, ...],
                 *, copy: bool) -> None:
        source = self.scope(source_id)
        target = self.scope(target_id)
        if source.kind != target.kind:
            raise ValueError("cannot transfer signals between analog and digital scopes")
        if len(set(target.signals).union(signals)) > (12 if target.kind == "digital" else 8):
            raise ValueError("scope has too many signals")
        if any(signal_id not in source.signals for signal_id in signals):
            raise ValueError("selected signal is not in source scope")
        for signal_id in signals:
            if signal_id not in target.signals:
                target.signals.append(signal_id)
            if not copy and source is not target:
                source.signals.remove(signal_id)

    def delete_scope(self, scope_id: str) -> None:
        self.scopes = [scope for scope in self.scopes if scope.id != scope_id]

    def move_scope(self, scope_id: str, delta: int) -> None:
        current = next(i for i, scope in enumerate(self.scopes) if scope.id == scope_id)
        destination = current + delta
        if destination < 0 or destination >= len(self.scopes):
            return
        self.scopes[current], self.scopes[destination] = (
            self.scopes[destination], self.scopes[current])

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "names": {key: signal.name for key, signal in self.signals.items()
                      if signal.name and not key.startswith("math:")},
            "active": [key for key, signal in self.signals.items()
                       if signal.active and not key.startswith("math:")],
            "math": [
                {"id": item.id, "handle": item.handle,
                 "name": self.signals[item.id].name, "expression": item.expression,
                 "unit": item.unit}
                for item in self.math.values()
            ],
            "scopes": [
                {"id": scope.id, "name": scope.name, "signals": scope.signals,
                 "collapsed": scope.collapsed, "kind": scope.kind}
                for scope in self.scopes
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Workspace:
        if not isinstance(data, dict):
            raise TypeError("malformed workspace")
        if data.get("version") != 1:
            raise ValueError("unsupported workspace version")
        obj = cls()
        names = data.get("names")
        active = data.get("active")
        math_items = data.get("math", [])
        scopes = data.get("scopes")
        if (not isinstance(names, dict) or not isinstance(active, list)
                or not isinstance(scopes, list) or not isinstance(math_items, list)):
            raise TypeError("malformed workspace")
        if len(scopes) > 32 or len(active) > len(obj.signals) or len(math_items) > 32:
            raise ValueError("workspace exceeds limits")
        for key, name in names.items():
            if key not in obj.signals or not isinstance(name, str):
                raise ValueError("unknown channel name")
            obj.rename(key, name)
        if any(not isinstance(key, str) or key not in obj.signals for key in active):
            raise ValueError("unknown active channel")
        for signal in obj.signals.values():
            signal.active = signal.id in active
        handles: set[str] = set()
        for item in math_items:
            if not isinstance(item, dict):
                raise TypeError("malformed math channel")
            identifier = item.get("id")
            handle = item.get("handle")
            name = item.get("name")
            expression = item.get("expression")
            unit = item.get("unit")
            if (not isinstance(identifier, str) or not identifier.startswith("math:")
                    or identifier in obj.signals or not isinstance(handle, str)
                    or not handle.startswith("M") or not handle[1:].isdigit()
                    or handle in handles or not isinstance(name, str)
                    or not isinstance(expression, str) or not isinstance(unit, str)
                    or unit not in {"V", "1"}):
                raise ValueError("malformed math channel")
            obj.signals[identifier] = Signal(identifier, active=True)
            obj.rename(identifier, name)
            obj.math[identifier] = MathDefinition(identifier, handle, expression, unit, ())
            handles.add(handle)
        for identifier, item in tuple(obj.math.items()):
            program = compile_math(item.expression, obj._symbols())
            obj._check_math_sources_active(program.dependencies)
            if program.unit != item.unit:
                raise ValueError("math unit mismatch")
            obj.math[identifier] = MathDefinition(
                identifier, item.handle, item.expression, item.unit, program.dependencies)
            obj._math_programs[identifier] = program
        obj._check_math_cycles()
        obj.scopes = []
        ids: set[str] = set()
        for item in scopes:
            if not isinstance(item, dict):
                raise TypeError("malformed scope")
            scope_id, name, signals = item.get("id"), item.get("name"), item.get("signals")
            if (not isinstance(scope_id, str) or not scope_id.startswith("scope:")
                    or scope_id in ids or not isinstance(name, str) or len(name) > 64
                    or not isinstance(signals, list)):
                raise ValueError("malformed scope")
            kind = item.get("kind", "analog")
            if kind not in {"analog", "digital"}:
                raise ValueError("unknown scope kind")
            if len(signals) > (12 if kind == "digital" else 8):
                raise ValueError("scope has too many signals")
            if any(not isinstance(key, str) or key not in obj.signals
                   or not obj._plottable(key, kind) or not obj.signals[key].active for key in signals):
                raise ValueError("scope references inactive or unsupported channel")
            ids.add(scope_id)
            obj.scopes.append(Scope(scope_id, name, list(dict.fromkeys(signals)),
                                    bool(item.get("collapsed", False)), kind=kind))
        return obj
