"""A minimal stand-in for Qt's signal/slot machinery, with no Qt underneath.

``Link`` and ``GameBridge`` never needed threading, queuing or introspection --
just "call these functions when this happens", which is all a Qt
``Signal`` was ever used for here. Pulling in PySide6 for that dragged a GUI
toolkit into a headless bridge process.

Emission stays synchronous, on whichever thread calls ``emit``. That is
deliberate: there are no widgets left to be thread-unsafe about, and queueing
records for a main loop to drain would add that loop's own poll interval to
every sample's latency -- in a rhythm game, the difference between a hit and
a miss.
"""

from __future__ import annotations


class _Bound:
    """A signal bound to one instance: a list of callables to notify."""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class Signal:
    """Declared on the class, bound per instance -- like a Qt Signal was.

    The constructor arguments (types) are accepted and ignored; nothing here
    checks them, exactly as nothing outside Qt ever did either.
    """

    def __init__(self, *_types) -> None:
        pass

    def __set_name__(self, owner, name) -> None:
        self._name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        bound = instance.__dict__.get(self._name)
        if bound is None:
            bound = _Bound()
            instance.__dict__[self._name] = bound
        return bound


class Emitter:
    """Base class for anything that declares :class:`Signal` attributes."""

    def __init__(self, *a, **k) -> None:
        pass
