"""The profile registry: what an onboarding document is, by kind.

The engine does not know the name of a single profile kind. A kind is an opaque
string; what makes it mean something is a *contract* registered against it — the
model that validates the document, and the authoring filename it is written in.

That is the difference between a product and a platform. Before this, four
places enumerated the kinds by hand: a `PROFILE_KINDS` tuple, a `CHECK(... IN
(...))` in two store schemas, a chain of `if kind == ...` branches that decoded a
stored row, and a filename map. Adding a fourth object meant editing all four,
and forgetting one produced a run that half-worked. Now adding an object is
`register(MyProfile)` — or, if it ships as a skill or plugin, importing the
module that calls it.

The built-in registrations below are a convenience, not a constraint: they name
the contracts this distribution ships, and `register` is the open door next to
them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: A kind is a slug, which is the only thing the engine can check about it.
KIND = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

class _Registry:
    """Kind → contract. Open at runtime, seeded with what ships here."""

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._loaded = False

    def register(self, model: Any, *, kind: str = "") -> str:
        """Teach the registry one profile contract.

        The kind and the authoring filename come from the contract itself
        (``profile_kind`` and ``authoring_file``), so a contract states its own
        identity rather than being described from outside. An explicit ``kind``
        is allowed for a contract that cannot carry the ClassVar.
        """
        name = str(kind or getattr(model, "profile_kind", "") or "").strip()
        if not KIND.fullmatch(name):
            raise ValueError(
                f"profile kind {name!r} must be a slug (a-z, 0-9, _)"
            )
        self._models[name] = model
        return name

    def model(self, kind: str) -> Any:
        """The contract registered for ``kind``, or None when none is.

        None is not an error. An unregistered kind is data — a document some
        other tool wrote — and it reads back as itself rather than being
        refused by a list the engine happened to ship with.
        """
        self._load_builtins()
        return self._models.get(str(kind or ""))

    def authoring_file(self, kind: str) -> str:
        """The filename this kind is authored in, from its own contract."""
        model = self.model(kind)
        return str(getattr(model, "authoring_file", "") or "") if model else ""

    def kinds(self) -> list[str]:
        self._load_builtins()
        return sorted(self._models)

    def _load_builtins(self) -> None:
        """Import the contracts this distribution ships, once, lazily.

        A module that cannot import is skipped rather than fatal, so an install
        carrying only some of the lane contracts still onboards the objects it has.
        """
        if self._loaded:
            return
        self._loaded = True
        for module_name, class_name in (
            ("harness_fleet.profile", "IdealCompanyProfile"),
            ("harness_fleet.partner", "IdealPartnerProfile"),
            ("harness_fleet.profile", "IdealEmployerProfile"),
        ):
            try:
                module = __import__(module_name, fromlist=[class_name])
                self.register(getattr(module, class_name))
            except Exception:
                continue


_registry = _Registry()


def register(model: Any, *, kind: str = "") -> str:
    """Register a profile contract. The open door for a new object."""
    return _registry.register(model, kind=kind)


def model_for(kind: str) -> Any:
    return _registry.model(kind)


def authoring_file(kind: str) -> str:
    return _registry.authoring_file(kind)


def kinds() -> list[str]:
    return _registry.kinds()


def _decode(model: Any, target: Path) -> Any:
    """One document, decoded by one contract.

    A contract that ships its own ``load`` uses it. One that does not — a model
    somebody registered five minutes ago — is read as JSON and validated, so the
    only thing registering an object requires is the object.
    """
    loader = getattr(model, "load", None)
    if callable(loader):
        try:
            return loader(target)
        except Exception:
            return None
    try:
        import json

        return model.model_validate(json.loads(target.read_text(encoding="utf-8")))
    except Exception:
        return None


def load(path: Path | str, kind: str = "") -> Any:
    """Load an onboarding document, decoded by contract rather than by branch.

    A named kind uses its registered contract. Without one, every registered
    contract is tried in turn: each forbids unknown fields, so the document that
    validates is unambiguously the one it is. A document no contract accepts
    still reads as a plain mapping, because refusing data the engine merely does
    not know is worse than carrying it.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return None
    named = model_for(kind) if kind else None
    if named is not None:
        return _decode(named, target)
    for candidate in (model_for(name) for name in kinds()):
        decoded = _decode(candidate, target)
        if decoded is not None:
            return decoded
    try:
        import json

        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
