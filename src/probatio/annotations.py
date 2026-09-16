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

  A type that already keeps the metadata under names of its own *can* expose a
  property of that name over them, since probatio only ever reads and writes the
  attribute. That form is a poor default and the docs do not lead with it: a
  property over fixed fields silently drops any key it does not know, it costs
  several times a slot read on every rebuilt container, and it is the shape that
  makes ``Object`` unable to carry (see ``_ObjectValidator``). Prefer the slot.
- Wherever probatio rebuilds a *container*, it carries the annotations across. The
  rebuilt value is the original in contents *and* in what it was annotated with.
  ``Object`` is the exception: it constructs from validated attributes rather than
  filling a container, so it carries nothing.
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
from types import MappingProxyType
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
        # way to the contents is read-only, through the proxy.
        self._data: Mapping[str, Any] = MappingProxyType(data)

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


def supports_annotations(value: Any) -> bool:
    """Report whether annotations attached to ``value`` would stick.

    True when the value can be *written* to, not merely read from: its type declares
    the attribute in ``__slots__``, exposes a settable property (or another data
    descriptor) of that name, or gives its instances an ordinary ``__dict__``. A
    plain ``dict``, ``list``, ``str`` or ``int`` does none of those, so annotating
    one is a no-op and this returns False. So is a property with no setter, which can
    be read but never carries anything across a rebuild.

    The one thing it cannot see through is a ``__setattr__`` that rejects the write
    itself, which is only knowable by trying. A frozen dataclass is the case worth
    naming: it has a ``__dict__``, so this reports True, and its ``__setattr__``
    raises ``FrozenInstanceError``, which subclasses ``AttributeError`` and is
    therefore swallowed like any other refusal. A class object reports False: its
    ``__dict__`` is a read-only proxy, and probatio is asked about values.

    Nor can it see whether a carrier keeps what it is given. A property over two
    named fields accepts the write and stores only those two, so it reports True and
    is right to, while an annotation under any other key is dropped. Only reading
    back with ``annotations_of`` shows that.
    """
    descriptor = getattr(type(value), ANNOTATIONS_ATTR, None)
    if isinstance(descriptor, property):
        return descriptor.fset is not None
    if descriptor is not None and hasattr(type(descriptor), "__set__"):
        # A slot's member descriptor, or any other data descriptor, accepts a write.
        return True
    # Nothing on the class takes the write, so it can only land in an instance
    # ``__dict__``. A class object's ``__dict__`` is a mappingproxy, not a dict, so
    # this is also what keeps a built-in type from reporting True.
    return isinstance(getattr(value, "__dict__", None), dict)


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
    should not fail over metadata it could not attach. A value already carrying
    something that is not a mapping is a broken carrier, and raises ``TypeError``
    the same way ``annotations_of`` does.
    """
    if not annotations and not extra:
        return value
    # Read through ``annotations_of`` so a value carrying something that is not a
    # mapping reports that plainly rather than failing inside a dict copy.
    current = annotations_of(value)
    merged = (
        Annotations(annotations, **extra)
        if current is None
        else current.merge(annotations, **extra)
    )
    try:  # noqa: SIM105 - contextlib.suppress costs 5x here (91 ns against 18 ns)
        setattr(value, ANNOTATIONS_ATTR, merged)
    except (AttributeError, TypeError):
        # The value has nowhere to put them: no slot and no ``__dict__``, a
        # read-only property, or a built-in type, which refuses with a TypeError
        # rather than an AttributeError. Documented as a no-op, not an error.
        pass
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
    """
    annotations = getattr(source, ANNOTATIONS_ATTR, None)
    if annotations is None:
        return target
    # This runs once per rebuilt container, so it is the one genuinely hot line in
    # this module. A ``try`` that does not raise is free; ``contextlib.suppress``
    # builds an object and calls into it every time, measured at 91 ns against 18 ns.
    try:  # noqa: SIM105
        setattr(target, ANNOTATIONS_ATTR, annotations)
    except (AttributeError, TypeError):
        # The target has nowhere to put them: a carrier whose annotation property is
        # read-only, or whatever a validator passed in. The engine's own rebuild
        # sites never land here, because they only call this with a fresh instance of
        # a class that just proved it can hold the attribute. A no-op, not an error.
        pass
    return target
