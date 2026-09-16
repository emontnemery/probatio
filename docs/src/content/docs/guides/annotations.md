---
title: Annotations
description: Metadata a value carries beside its contents, how a type opts in, and how Probatio keeps it across every container rebuild.
---

Some data arrives knowing more about itself than its contents say. A YAML loader
knows the file and the line every mapping came from. A request parser knows
which part of a multipart body a field was in. A migration knows a key used to
be spelled differently. That knowledge belongs to the value, not to the schema,
and it is exactly what a good error message needs.

Validation is hostile to it. A mapping or sequence schema rebuilds its input, so
whatever the original held beside its items does not come along. Annotations are
Probatio's answer: a small, defined model for that metadata, carried across
every container rebuild. (`Object` is the one exception, for a reason covered
below.)

## What validation loses

Here is a `dict` subclass that records the line it was loaded from, the shape a
YAML loader produces:

```python
from probatio import Schema


class Node(dict):
    __slots__ = ("line",)


node = Node({"name": "kitchen"})
node.line = 12

validated = Schema({"name": str})(node)

type(validated) is Node  # True
hasattr(validated, "line")  # False
```

The class survived. The line did not. Probatio preserves a `dict` subclass as
its own class (voluptuous does too), but the rebuilt container is a _fresh,
empty_ instance of that class: the items are copied over one at a time, and
nothing else is. The loss happens at every nesting depth, for every schema shape
that rebuilds, so no amount of care at the call site reaches it.

What that costs is a message. "option 'old_name' is deprecated" is worth
reading; "option 'old_name' is deprecated, at configuration.yaml line 12" is
worth acting on. The second one needs the line to survive validation.

## The model

`Annotations` is what a value carries: an immutable mapping of `str` keys to
anything. Build one from a mapping, from keyword arguments, or from both.

```python
from probatio import Annotations

where = Annotations(file="configuration.yaml", line=12)

where["line"]  # 12
dict(where)  # {'file': 'configuration.yaml', 'line': 12}
```

It reads like any mapping (`in`, `len`, `get`, iteration) and it cannot be
changed. To add to a set of annotations, `merge` returns a new one, leaving the
original alone:

```python
from probatio import Annotations

where = Annotations(file="configuration.yaml", line=12)
moved = where.merge(line=14)

moved["line"]  # 14
where["line"]  # 12
```

The immutability is load-bearing. Probatio hands the _same_ `Annotations` object
to a rebuilt container rather than copying it, which is what keeps the carry
cheap. That is only safe because nothing can change it afterwards through either
reference, so the guarantee is enforced rather than advised: the keys and values
are copied into a mapping no one else holds, reachable only read-only.

A value carries its annotations in one attribute, `__probatio_annotations__`,
also exported as `ANNOTATIONS_ATTR`. That attribute is the whole protocol: no
registration, no base class, no method to implement.

## Opting a type in

A type opts in by making room for that attribute. There are three ways, and
Probatio only ever reads and writes the attribute, so all three work.

For a `__slots__` type, it is one line:

```python
from probatio import annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


annotations_of(annotate(Node(), line=1))["line"]  # 1
```

For a type whose instances have an ordinary `__dict__`, there is nothing to do
at all. It already has somewhere to put it:

```python
from probatio import annotate, annotations_of


class Node(dict):
    """No __slots__, so instances take arbitrary attributes."""


annotations_of(annotate(Node(), line=2))["line"]  # 2
```

A type that already stores the metadata under names of its own can expose a
property of that name over the fields it has, which means no change to how the
data is stored. Reach for this one last, for the reasons below it:

```python
from probatio import Annotations, Schema


class YamlNode(dict):
    __slots__ = ("config_file", "line")

    @property
    def __probatio_annotations__(self):
        return Annotations(
            file=getattr(self, "config_file", None),
            line=getattr(self, "line", None),
        )

    @__probatio_annotations__.setter
    def __probatio_annotations__(self, annotations):
        self.config_file = annotations.get("file")
        self.line = annotations.get("line")


node = YamlNode({"name": "kitchen"})
node.config_file = "configuration.yaml"
node.line = 12

validated = Schema({"name": str})(node)

validated.line  # 12
validated.config_file  # 'configuration.yaml'
```

A property carrier has three costs a slot does not, so prefer a slot unless you
cannot add one:

- **It keeps only what the fields behind it can hold.** The one above stores a
  `file` and a `line`, so `annotate(node, findings=[...])` is accepted, raises
  nothing, and the key is gone on the next read. `supports_annotations` reports
  `True` and is right to: the write did take. Nothing can report the loss, because
  whether a carrier keeps what it was given is only visible by reading it back.
- **It is several times slower.** The getter builds a fresh `Annotations` on every
  read, once per rebuilt container. Measured below.
- **`Object` will not carry it.** See the exception in the next section.

The property also needs a setter: a getter alone lets a value be read but never
written, so nothing is carried onto a rebuilt container and `supports_annotations`
reports `False` for it.

:::note[Why there is no mixin to inherit]
A base class would be the obvious convenience, and it is not offered on purpose.
`class Node(dict, Mixin)` with a non-empty `__slots__` on the mixin is a Python
layout conflict, so a shipped base class would fail for exactly the `dict` and
`list` subclasses this exists to serve. The attribute is the protocol instead.
:::

## Where Probatio carries them

Every place Probatio rebuilds a _container_ moves the original's annotations onto
the rebuilt one:

- a `dict` subclass rebuilt by a mapping schema,
- a `list`, `tuple`, or `set` subclass rebuilt by a sequence schema,
- `ExactSequence`.

`Object` is the exception, and deliberately so. It does not rebuild a container,
it constructs a new object out of the validated attributes, which means every
piece of validated state is an attribute. Writing the annotation attribute
afterwards can run a property setter, and that setter is free to write other
attributes: a carrier exposing its annotations over fields the same schema
validates would have the original, unvalidated values put back over the validated
ones. A container has no such exposure, because its items are not attributes. So
`Object` carries nothing, and an object's metadata survives by being attributes
the schema itself validates.

Nesting works at any depth, because each level is carried as it is rebuilt:

```python
from probatio import Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


class NodeList(list):
    __slots__ = ("__probatio_annotations__",)


port = annotate(Node({"port": 8123}), line=3)
servers = annotate(NodeList([port]), line=2)
config = annotate(Node({"servers": servers}), line=1)

validated = Schema({"servers": [{"port": int}]})(config)

annotations_of(validated)["line"]  # 1
annotations_of(validated["servers"])["line"]  # 2
annotations_of(validated["servers"][0])["line"]  # 3
```

Mapping keys keep their own. A key is passed through to the rebuilt mapping
rather than reconstructed, so an annotated key object arrives with everything it
carried:

```python
from probatio import Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


class Key(str):
    __slots__ = ("__probatio_annotations__",)


key = annotate(Key("name"), line=5)
validated = Schema({"name": str})(Node({key: "kitchen"}))

[annotations_of(key)["line"] for key in validated]  # [5]
```

## Reading and adding from a validator

Four helpers are the whole validator-facing API. `annotate` merges annotations
into what a value already carries and returns the value, so it reads well as the
last line of a validator:

```python
from probatio import Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


def mark_checked(value):
    return annotate(value, checked=True)


validated = Schema(mark_checked)(Node({"port": 8123}))

annotations_of(validated)["checked"]  # True
```

`annotations_of` reads them back, returning `None` when there are none. It
always returns an `Annotations`, even when the value stores a plain mapping in
the attribute, so a caller has one shape to work with, and it raises `TypeError`
for a value holding something that is not a mapping at all.
`supports_annotations` answers whether annotating a value would stick:

```python
from probatio import supports_annotations


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


supports_annotations(Node())  # True
supports_annotations({})  # False
supports_annotations("kitchen")  # False
```

It reports whether the value can be _written_ to, not merely read from, so a
property with no setter reports `False`. The one thing it cannot see through is a
`__setattr__` that rejects the write itself, which is only knowable by trying.

`carry_annotations` is for a validator that builds a new container itself.
Probatio carries annotations across the rebuilds it performs, but a
transformation you write is yours to carry:

```python
from probatio import Schema, annotate, annotations_of, carry_annotations


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


def drop_empty(value):
    kept = type(value)((key, item) for key, item in value.items() if item)
    return carry_annotations(value, kept)


config = annotate(Node({"name": "kitchen", "alias": ""}), line=7)
validated = Schema(drop_empty)(config)

dict(validated)  # {'name': 'kitchen'}
annotations_of(validated)["line"]  # 7
```

Because the engine propagates whatever it finds, an annotation a validator adds
survives every container rebuild above it, and the order in `All` does not
matter. Both of
these end with the loader's annotations and the validator's own:

```python
from probatio import All, Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


def mark_checked(value):
    return annotate(value, checked=True)


after = Schema(All({"port": int}, mark_checked))
before = Schema(All(mark_checked, {"port": int}))

dict(annotations_of(after(annotate(Node({"port": 8123}), line=1))))
# {'line': 1, 'checked': True}
dict(annotations_of(before(annotate(Node({"port": 8123}), line=1))))
# {'line': 1, 'checked': True}
```

That is what makes the error message from the start of this page possible. The
check runs after the mapping schema has rebuilt the config, and the annotations
are still there to read:

```python
from probatio import All, Invalid, MultipleInvalid, Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


def no_deprecated_options(value):
    if "old_name" not in value:
        return value
    where = annotations_of(value)
    if where is None:
        raise Invalid("option 'old_name' is deprecated, use 'name'")
    raise Invalid(
        f"option 'old_name' is deprecated, use 'name', "
        f"at {where['file']} line {where['line']}"
    )


schema = Schema(All({"old_name": str}, no_deprecated_options))
config = annotate(Node({"old_name": "kitchen"}), file="configuration.yaml", line=12)

try:
    schema(config)
except MultipleInvalid as err:
    print(err)
    # option 'old_name' is deprecated, use 'name', at configuration.yaml line 12
```

## The limits

Annotations are deliberately narrow, and the edges are worth knowing before you
rely on them.

**A value that cannot hold an attribute cannot be annotated.** A plain `dict`, a
`str`, an `int`: none of them make room for one. `annotate` and
`carry_annotations` return such a value unchanged rather than raise, so a
validator can call them without knowing what it was handed. The cost is that a
genuine mistake is quiet, which is what `supports_annotations` exists to check
for:

```python
from probatio import annotate, annotations_of

config = {"port": 8123}

annotate(config, line=1)  # {'port': 8123}
annotations_of(config)  # None
```

**A validator that destroys the type destroys the annotations with it.**
Returning `dict(value)`, a comprehension, or an accumulator produces a plain
`dict` or `list`, and no engine change can heal that. The fix is
`carry_annotations`, applied where the validator is written, as in `drop_empty`
above.

**`Coerce(dict)` loses them, correctly.** It is asked for a plain `dict` and it
returns one, and a plain `dict` holds no attributes. That is the coercion doing
its job, not a gap.

**A rebuild replaces rather than merges.** The engine moves the source's
annotations onto the rebuilt value, overwriting any the fresh instance gave
itself in its own constructor. The rebuilt value stands in for the original, so
the original's annotations are the right ones. A source carrying none leaves the
new instance's own alone.

```python
from probatio import Schema, annotate, annotations_of


class Stamped(dict):
    __slots__ = ("__probatio_annotations__",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        annotate(self, origin="constructor")


node = annotate(Stamped({"port": 8123}), origin="configuration.yaml")
validated = Schema({"port": int})(node)

annotations_of(validated)["origin"]  # 'configuration.yaml'
```

**Annotations are shared, not copied.** The rebuilt value gets the same
`Annotations` object as the original, which is safe precisely because an
`Annotations` cannot change:

```python
from probatio import Schema, annotate, annotations_of


class Node(dict):
    __slots__ = ("__probatio_annotations__",)


node = annotate(Node({"port": 8123}), line=1)
validated = Schema({"port": int})(node)

annotations_of(validated) is annotations_of(node)  # True
```

Annotations are also not general instance state. Probatio copies this one
attribute and nothing else, so a subclass keeping a cache, a lock, or a parent
pointer does not have it silently duplicated onto a new object.

## What it costs

The carry is one attribute read and one attribute write, once per rebuilt
container. A plain `dict` or `list` never reaches it at all: those paths return
the container they built without a carry.

Measured on CPython 3.14, per call to `carry_annotations`:

- a subclass that does not opt in: **24 ns**, the cheap kind of miss, since the
  type defines no such attribute,
- a slot carrier with the annotations set: **42 ns**,
- a slot carrier declared but _not_ set on this value: **99 ns**, because the slot
  descriptor exists and raises on the read,
- a property carrier: **328 ns**, because the getter builds an `Annotations` every
  time.

The third of those is worth designing around: if a loader declares the slot on
every node but sets it on only some, the unset ones are the expensive case, not
the cheap one. Have the loader set the attribute unconditionally.

In aggregate, on a 1500-entry nested config (1502 rebuilt containers), a `dict`
or `list` subclass that does not opt in costs about 22 ns per container, and a
slot carrier with its annotations set costs about 49 ns, roughly 0.07 ms for the
whole document.

## Where to next

- [Custom validators](/guides/custom-validators/): the callables that `annotate`
  and `carry_annotations` are written inside.
- [The validation model](/guides/validation-model/): why a returned value can
  differ from the input, and what rebuilding means.
- [Home Assistant](/recipes/home-assistant/): the case this was built for, with
  a YAML node class opting in.
