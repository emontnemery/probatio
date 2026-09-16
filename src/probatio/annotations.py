"""Annotations: metadata a value carries alongside its contents.

Some data arrives with more to say than its contents. A YAML loader knows the file
and line every mapping came from; a request parser knows which part of a multipart
body a field was in; a migration knows a key used to be called something else. That
knowledge belongs to the value, not to the schema, and it is exactly what a good
error message needs.

Validation is hostile to it. A mapping or sequence schema rebuilds its input, so
even when the class survives (probatio preserves ``dict`` and ``list`` subclasses,
matching voluptuous) the rebuilt container is a *fresh, empty* instance: the items
are carried over one by one and everything the original held beside them is gone.

This module is probatio's answer: a small, explicit model of that metadata.

- ``Annotations`` is the model. An immutable mapping of ``str`` to anything.
- A value carries its annotations in one attribute, ``__probatio_annotations__``
  (also available as ``ANNOTATIONS_ATTR``). A type opts in by making room for that
  attribute, which for a ``__slots__`` type is one line::

      class Node(dict):
          __slots__ = ("__probatio_annotations__",)

  A type with an ordinary ``__dict__`` needs no declaration at all. A ``tuple`` or
  ``bytes`` subclass cannot take the slot form (CPython refuses a non-empty
  ``__slots__`` on one), so it opts in by declaring no ``__slots__`` and using the
  ``__dict__`` it gets instead.

  A property of that name is *not* a third way. probatio writes the attribute on a
  value it has just validated, so it writes only where the write lands in the
  attribute itself; a setter is a call, it receives the container, and it could add
  a key a mapping schema never saw or replace an item a sequence schema checked.
  ``supports_annotations`` reports which values qualify.
- Wherever probatio rebuilds a value *as its own type*, it carries the annotations
  across: the mapping engine, the sequence engine, ``ExactSequence``, and ``Object``.
  The rebuilt value is the original in contents *and* in what it was annotated with.
  A rebuild that does not keep the type cannot keep the annotations either: a
  ``Mapping`` that is not a ``dict`` subclass validates to a plain ``dict``, and a
  plain ``dict`` has nowhere to hold them.
- A validator adds to them with ``annotate``, or moves them onto a value it built
  itself with ``carry_annotations``.

Annotations are deliberately *not* general instance state. probatio copies this one
attribute and nothing else, so a subclass keeping a cache, a lock, or a parent
pointer does not have it silently duplicated onto a new object.

Values that cannot hold an attribute (a plain ``dict``, a ``str``, an ``int``) also
cannot hold annotations. ``annotate`` and ``carry_annotations`` return such a value
unchanged rather than raise, so a validator can call them without knowing what it
was handed; ``supports_annotations`` answers the question for a caller that cares.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType, MemberDescriptorType
from typing import Any

# The one attribute a value carries its annotations in. The name is namespaced to
# probatio and has two trailing underscores, so Python's private-name mangling
# leaves it alone inside a class body and it can be written straight into
# ``__slots__``.
ANNOTATIONS_ATTR = "__probatio_annotations__"

__all__ = [
    "ANNOTATIONS_ATTR",
    "Annotations",
    "annotate",
    "annotations_of",
    "carry_annotations",
    "supports_annotations",
]


class Annotations(Mapping[str, Any]):
    """An immutable mapping of metadata attached to a value.

    Build one from a mapping, from keyword arguments, or from both::

        Annotations(file="app.yaml", line=12)
        Annotations({"file": "app.yaml"}, line=12)

    It is immutable on purpose. probatio hands the *same* ``Annotations`` object to
    a rebuilt container rather than copying it, which is what keeps the carry cheap;
    that is only safe because nothing can change it afterwards through either
    reference. Add to a set of annotations with ``merge``, which returns a new one.

    The immutability is enforced, not a convention: the keys and values are copied
    into a dict no one else holds, and that dict is reached only through a
    ``MappingProxyType``, so there is no route back to a mutable view of it.
    """

    __slots__ = ("_data",)

    # Annotated, not assigned: the slot is filled once in ``__init__`` through
    # ``object.__setattr__``, since this class refuses ordinary attribute writes.
    _data: Mapping[str, Any]

    def __init__(
        self,
        annotations: Mapping[str, Any] | None = None,
        /,
        **extra: Any,
    ) -> None:
        """Build annotations from a mapping, keyword arguments, or both.

        A keyword argument wins over the same key in ``annotations``. The mapping is
        copied, so a later change to it does not reach into the built object.
        """
        data: dict[str, Any] = dict(annotations) if annotations is not None else {}
        if extra:
            data.update(extra)
        # The proxy wraps a dict that leaves this frame unreferenced, so the only
        # way to the contents is read-only, through the proxy. Set through
        # ``object.__setattr__`` because this class refuses attribute writes; this is
        # the one write, and it happens before anything else can see the object.
        object.__setattr__(self, "_data", MappingProxyType(data))

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse every attribute write: an ``Annotations`` is built once.

        Without this the storage could be rebound (``annotations._data = {...}``),
        which matters more than it looks: probatio hands the *same* object to every
        value it carries onto, and a loader is encouraged to share one across every
        value from the same place, so a single rebinding would change what all of
        them report.
        """
        message = f"{type(self).__name__} is immutable"
        raise AttributeError(message)

    def __delattr__(self, name: str) -> None:
        """Refuse attribute deletion, for the same reason as ``__setattr__``."""
        message = f"{type(self).__name__} is immutable"
        raise AttributeError(message)

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild through the constructor, since a ``MappingProxyType`` cannot pickle.

        The default slot-based reduction would try to pickle the proxy itself and
        fail. Handing the constructor a plain dict of the contents round-trips to an
        equal ``Annotations``, and makes ``copy`` and ``deepcopy`` work too.
        """
        return (type(self), (dict(self._data),))

    def __getitem__(self, key: str) -> Any:
        """Return the value annotated under ``key``."""
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate the annotation keys."""
        return iter(self._data)

    def __len__(self) -> int:
        """Return how many annotations there are."""
        return len(self._data)

    def __repr__(self) -> str:
        """Render as ``Annotations({...})``, showing the annotations themselves."""
        # ``dict(...)``, not the proxy's own repr, which would spell the storage
        # (``mappingproxy({...})``) rather than the annotations.
        return f"{type(self).__name__}({dict(self._data)!r})"

    def merge(
        self,
        annotations: Mapping[str, Any] | None = None,
        /,
        **extra: Any,
    ) -> Annotations:
        """Return a new ``Annotations`` with these updated by the given ones.

        The incoming values win on a shared key. This object is left untouched.
        """
        if not annotations and not extra:
            return self
        merged = dict(self._data)
        if annotations is not None:
            merged.update(annotations)
        if extra:
            merged.update(extra)
        return Annotations(merged)


# How to write the annotation attribute on an instance of a given class, or None when
# it cannot hold one. A member descriptor writes its own slot; ``_INSTANCE_DICT`` means
# write the instance ``__dict__`` directly. Capturing the mechanism, rather than
# checking a class and then calling ``setattr``, is what makes the cache safe: whatever
# the class becomes afterwards, the write still lands in the attribute itself and runs
# nothing the carrier wrote. Bounded, and cleared wholesale when it fills, so a program
# minting classes cannot grow it without limit or pin them alive forever.
_WRITERS: dict[Any, Any] = {}
_WRITERS_LIMIT = 512
_INSTANCE_DICT = object()
_UNKNOWN = object()


def _annotation_writer(cls: Any) -> Any:
    """Return how to write the annotation attribute on ``cls``, or None if it cannot.

    probatio writes this attribute on values it has just validated, so it writes only
    where the write *is* a write. A property, or any other descriptor the carrier
    defined, takes it as a call and receives the value itself: such a setter could add
    a key a mapping schema never saw or replace an item a sequence schema just checked,
    and the schema would then return content it never approved. A type overriding
    ``__setattr__`` is refused for the opposite reason: the write could be routed past
    it, and a type that intercepts its own writes (a frozen dataclass, say) means them
    to be intercepted.

    The class is inspected by reading namespaces along ``__mro__`` rather than calling
    ``getattr`` on it, which would run a custom descriptor's ``__get__``: user code
    that can raise, or report something other than what a write would do.
    """
    cached = _WRITERS.get(cls, _UNKNOWN)
    if cached is not _UNKNOWN:
        return cached
    if len(_WRITERS) >= _WRITERS_LIMIT:
        _WRITERS.clear()
    writer = _compute_annotation_writer(cls)
    _WRITERS[cls] = writer
    return writer


def _compute_annotation_writer(cls: Any) -> Any:
    """Work out the uncached answer for ``_annotation_writer``."""
    # A user-defined ``__setattr__`` anywhere but ``object`` means the type wants every
    # write to go through it, and probatio will not route around that.
    if any("__setattr__" in vars(base) for base in cls.__mro__[:-1]):
        return None
    for base in cls.__mro__:
        namespace = vars(base)
        if ANNOTATIONS_ATTR in namespace:
            found = namespace[ANNOTATIONS_ATTR]
            # A slot's member descriptor writes that slot and nothing else. Anything
            # else on the class is code the carrier wrote.
            return found if isinstance(found, MemberDescriptorType) else None
    # Nothing on the class, so the write can only land in an instance ``__dict__``.
    if any("__dict__" in vars(base) for base in cls.__mro__[:-1]):
        return _INSTANCE_DICT
    return None


def supports_annotations(value: Any) -> bool:
    """Report whether probatio can attach annotations to ``value`` and carry them.

    True when the annotation attribute is somewhere the value simply holds it: a name
    in its type's ``__slots__``, or an ordinary instance ``__dict__``. Those are the
    two opt-in forms, and they are the only ones.

    False for everything else, including some that can technically be written to. A
    property of that name reports False whether or not it has a setter, and so does
    any other descriptor the type defines, because probatio will not run a carrier's
    code over a value it has just validated. A type overriding ``__setattr__`` reports
    False because it means to intercept its own writes, and probatio will not route
    around that; a frozen dataclass is the case worth naming. A plain ``dict``,
    ``list``, ``str`` or ``int`` has nowhere to put the attribute at all, and a class
    object's ``__dict__`` is a read-only proxy, so those report False too.

    What it cannot tell you is whether a write that does land will be *kept*. A
    carrier is free to store less than it was given, and only reading it back with
    ``annotations_of`` shows that.
    """
    return _annotation_writer(type(value)) is not None


def annotations_of(value: Any) -> Annotations | None:
    """Return the annotations attached to ``value``, or None if it has none.

    The result is always an ``Annotations``, even when the value stores a plain
    mapping in the attribute, so a caller gets one shape to work with. A value
    holding something that is not a mapping is a broken carrier, and raises
    ``TypeError`` rather than being silently read as empty.
    """
    raw = getattr(value, ANNOTATIONS_ATTR, None)
    if raw is None:
        return None
    if type(raw) is Annotations:
        return raw
    if not isinstance(raw, Mapping):
        message = (
            f"{ANNOTATIONS_ATTR} on {type(value).__name__} holds "
            f"{type(raw).__name__}, which is not a mapping"
        )
        raise TypeError(message)
    return Annotations(raw)


def annotate[T](
    value: T,
    annotations: Mapping[str, Any] | None = None,
    /,
    **extra: Any,
) -> T:
    """Merge annotations into the value's own and return the value.

    This is how a validator adds to what a value carries::

        def normalize_port(value):
            return annotate(int(value), normalized=True)

    Existing annotations are kept; the incoming ones win on a shared key. Adding
    nothing (no mapping and no keywords, or an empty mapping) changes nothing, so a
    validator can pass a mapping it assembled without checking whether it came out
    empty. The value is returned either way, so this reads well as the last line of
    a validator. A
    value that cannot hold annotations is returned unchanged, because a validator
    should not fail over metadata it could not attach, and that covers a carrier
    whose setter raises something of its own.

    Reading is not swallowed the way ``carry_annotations`` swallows it. A value
    already carrying something that is not a mapping raises ``TypeError`` the same
    way ``annotations_of`` does, and a carrier whose getter raises surfaces that.
    This is an explicit call by the code that wants the annotation, so a broken
    carrier is worth hearing about, and proceeding would mean silently discarding
    whatever the value already had. The engine's own carry has neither luxury: it
    runs behind every rebuild and must never turn a carrier's fault into a
    validation failure.
    """
    if not annotations and not extra:
        return value
    # The same rule the engine's carry follows, so one protocol governs both: a value
    # probatio would not carry onto is not one it annotates either.
    writer = _annotation_writer(type(value))
    if writer is None:
        return value
    # Read through ``annotations_of`` so a value carrying something that is not a
    # mapping reports that plainly rather than failing inside a dict copy.
    current = annotations_of(value)
    merged = (
        Annotations(annotations, **extra)
        if current is None
        else current.merge(annotations, **extra)
    )
    try:
        if writer is _INSTANCE_DICT:
            value.__dict__[ANNOTATIONS_ATTR] = merged
        else:
            writer.__set__(value, merged)
    except Exception:  # noqa: BLE001 - a carrier refusing the write is not an error
        # The slot or instance dict would not take it. A no-op, not an error, the
        # same way ``carry_annotations`` treats it.
        return value
    return value


def carry_annotations[T](source: Any, target: T) -> T:
    """Copy ``source``'s annotations onto ``target`` and return ``target``.

    Use it in a validator that builds a new value from an old one, so what the old
    one was annotated with is not lost in the transformation::

        def drop_empty(value):
            kept = type(value)((k, v) for k, v in value.items() if v)
            return carry_annotations(value, kept)

    This replaces whatever ``target`` carried rather than merging into it: it moves
    a value's annotations onto its replacement. Both the no-annotations case and the
    cannot-hold-annotations case leave ``target`` alone.

    The annotations object itself is shared, not copied. That is safe for the
    ``Annotations`` this module defines, which cannot change. A carrier that chooses
    to store some other, mutable mapping shares that mapping between the two values.

    Unlike ``annotations_of`` and ``annotate``, this moves whatever it finds without
    checking that it is a mapping at all. It is the one line here that runs per
    rebuilt container, and a type check on every one of them would cost more than it
    is worth. A source carrying something that is not a mapping therefore spreads it
    rather than being reported, until something reads it through ``annotations_of``.

    It never raises for a carrier's own reasons. Both ends are ordinary attribute
    access, so both can run code the carrier wrote: a property getter on ``source``,
    a property setter or a ``__setattr__`` on ``target``. Whatever that code raises
    is swallowed and the carry is abandoned. This is not politeness, it is the
    safe-validator contract (see ``validators/_base.py``): ``ExactSequence`` and the
    mapping and sequence engines all call this, and a built-in validator that leaked
    a carrier's ``RuntimeError`` would escape the ``MultipleInvalid`` a caller
    catches. Failing to move metadata is not a statement about the data, so it is
    never a validation failure. ``BaseException`` still propagates.
    """
    # The whole read-and-write is guarded, not just the write: a property getter on
    # ``source`` can raise too. A ``try`` that does not raise is free, so this costs
    # the hot path nothing; ``contextlib.suppress`` would build an object and call
    # into it every time, measured at 91 ns against 18 ns.
    try:
        # Inside the guard: working out how to write the attribute reads the target's
        # class, and a hostile metaclass can raise from that alone.
        writer = _annotation_writer(type(target))
        if writer is None:
            return target
        annotations = getattr(source, ANNOTATIONS_ATTR, None)
        if annotations is not None:
            # Inlined rather than routed through a helper: this is the one line here
            # that runs per rebuilt value, and the call cost showed up in the carry.
            if writer is _INSTANCE_DICT:
                target.__dict__[ANNOTATIONS_ATTR] = annotations
            else:
                writer.__set__(target, annotations)
    except Exception:  # noqa: BLE001 - a carrier's own code must not fail validation
        return target
    return target
