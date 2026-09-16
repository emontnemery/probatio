---
title: Home Assistant
description: Use Probatio where Home Assistant uses voluptuous, with or without touching internals.
---

Home Assistant validates its configuration with voluptuous, and it does so
heavily. The `config_validation` helper is one of the largest voluptuous
consumers there is. Probatio mirrors the voluptuous API, so it can stand in.

## The simple case: change the import

Home Assistant code does not import voluptuous names one by one. The idiom is
`import voluptuous as vol`, with every schema written against the `vol.`
namespace: `vol.Schema`, `vol.Required`, `vol.Optional`, `vol.All`. That idiom
makes the migration a one-line diff, with every `vol.` reference untouched:

```diff
-import voluptuous as vol
+import probatio as vol
```

The rest of the file does not change. This runs on Probatio as-is:

```python
import probatio as vol

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Optional("port", default=8123): int,
    }
)

CONFIG_SCHEMA({"name": "living-room"})  # {'name': 'living-room', 'port': 8123}
```

The names, signatures, and behavior match. `Required`, `Optional`, `All`, `Any`,
`Coerce`, `Range`, `In`, the markers, the error classes: same surface, fresh
implementation.

## The harder case: code that imports voluptuous internals

Not everything imports the public API. Some dependencies reach into voluptuous
_internals_. Home Assistant's `annotatedyaml`, for example, imports
`voluptuous.schema_builder._compile_scalar` directly. You cannot change the
import in code you do not own.

For that, Probatio ships `install_as_voluptuous`. Call it once at process
startup, before anything imports voluptuous:

<!-- verify: skip -->

```python
from probatio.compat import install_as_voluptuous

install_as_voluptuous()
```

It registers a small voluptuous-shaped shim, backed by Probatio, into
`sys.modules` under the `voluptuous` name, so every later `import voluptuous`
resolves to Probatio for the rest of the process. It is process-wide, so call it
from an application entry point, not from library code, and call it early: if a
real voluptuous was already imported, it is shadowed and a `RuntimeWarning` is
emitted, since references already taken to the real module will not update. The
[Compatibility](/getting-started/compatibility/) page covers exactly what it
registers and when to call it.

## Keeping the source file and line

Home Assistant loads YAML through `annotatedyaml`, which records the file and
the line every mapping came from on its own node classes. Validation does not
keep them: a schema that rebuilds a mapping produces a fresh instance of the
node class, holding the validated items and nothing else. A rule like
`cv.deprecated` then has no way to say where in the configuration the deprecated
option was written.

Probatio's [annotations](/guides/annotations/) close that. A node class says
where its metadata lives, and every container rebuild carries it, at every
nesting depth.

The opt-in is one slot on the node class, and the loader writing what it knows
into the annotations:

```python
from probatio import Schema, annotate, annotations_of


class NodeDictClass(dict):
    __slots__ = ("__probatio_annotations__",)


# What the loader does once per node, where it already records the location.
node = annotate(
    NodeDictClass({"name": "kitchen"}),
    file="configuration.yaml",
    line=12,
)

validated = Schema({"name": str})(node)

annotations_of(validated)["line"]  # 12
annotations_of(validated)["file"]  # 'configuration.yaml'
```

That is the whole change on the `annotatedyaml` side: one slot, and one
`annotate` call where `__config_file__` and `__line__` are set today. Note that
it puts the location _into_ the annotations. Adding the slot alone does not help,
because `__config_file__` and `__line__` are separate slots and Probatio carries
only the one attribute it knows about.

### The readers have to move with it

The location now lives in the annotations, so the code that reads it back has to
read it from there. In Home Assistant that is four places:

- `homeassistant/config.py`, `find_annotation`
- `homeassistant/scripts/check_config.py`
- `homeassistant/helpers/config_validation.py`, `cv.deprecated`
- `homeassistant/components/mqtt/entity.py`

Each reads `__config_file__` and `__line__` off the node today, and each becomes
an `annotations_of(value)` lookup. A node class can keep the two old slots
populated during a transition, but a value that has been through a schema will
only have the annotations, so the readers are what makes `cv.deprecated` say
"near configuration.yaml:12" again.

A property over the existing `__config_file__` and `__line__` fields is possible,
and it is tempting because it leaves the loader and all four readers alone. It is
not the recommended path: such a property silently drops any annotation key that
is not `file` or `line`, it costs several times a slot read on every rebuilt
container, and `Object` will not carry it. The
[annotations guide](/guides/annotations/) has the details.

The opt-in lives in the node class, not in the schemas. Nothing in
`config_validation` changes to make the carry happen, and a schema that never
sees an annotated value behaves exactly as it did before.

This covers the metadata half of the problem, and only that half. A validator
that returns `dict(value)`, a comprehension, or an accumulator hands back a
plain `dict`, which is no longer a node class and can hold no attributes at all.
No engine change reaches that; the fix is `carry_annotations` where such a
validator is written, as the [annotations guide](/guides/annotations/) shows.

## It is tested against the real thing

This is not a paraphrase of compatibility. Probatio is validated against Home
Assistant's own `config_validation` test suite, with voluptuous swapped out for
Probatio through `install_as_voluptuous`. As of July 2026, 136 of the 142 tests
pass; the harness in `compat/home_assistant/` in the repository is the source
of truth for the current count. The 6 that fail all assert the exact
voluptuous-rendered error string, which Probatio deliberately renders
differently (a dotted path instead of `@ data[...]`, see the
[compatibility matrix](/reference/compatibility-matrix/)). The error attributes
those tests could assert on instead (`path`, `error_message`, the error class)
all match.

Getting there surfaced real compatibility gaps that isolated unit tests had
missed, like `Remove(key)` keeping a value that fails its schema, and the
`error_type` tag on a failed mapping value. Those are fixed. The harness
reproduces the run against a Home Assistant checkout.

## Where to next

- [Migrating from voluptuous](/getting-started/migrating-from-voluptuous/): the
  general swap, beyond Home Assistant.
- [Annotations](/guides/annotations/): the file and line a value carries, and
  how a type opts in.
- [Validating a config file](/recipes/config-file/): a worked end-to-end example.
- [Error handling](/guides/error-handling/): paths, multiple errors, and readable
  messages.
