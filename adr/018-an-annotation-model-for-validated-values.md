# ADR-018: An annotation model for validated values

**Date**: 2026-09-16
**Status**: Accepted

**Context**: Some data arrives knowing more about itself than its contents say. A
YAML loader knows the file and the line every mapping came from. A request parser
knows which part of a multipart body a field was in. A migration knows a key used
to be spelled differently. That knowledge is what turns "expected a string" into
"expected a string, near configuration.yaml:12", so it is worth keeping.

Validation loses it. A mapping or sequence schema rebuilds its input, and while
probatio preserves `dict` and `list` subclasses (matching voluptuous, see
`_engine.py`), the rebuilt container is a fresh, empty instance of that class: the
items are copied over one at a time and everything the original held beside them is
gone. Home Assistant hit this concretely. `annotatedyaml` records the source file
and line in `__slots__` on its node classes, and every schema that rebuilds a
mapping drops them, which is why `cv.deprecated` stopped being able to say where
the deprecated option was. The loss is not confined to the top level: it happens at
every nesting depth, for every schema shape that rebuilds, so no amount of care at
the call site reaches it.

Three ways to close it were considered.

1. **Carry the whole instance state.** Read the source's default state through
   `object.__getstate__` (which reports `__dict__` and set `__slots__`) and write it
   onto the rebuilt container. It needs no cooperation from the data's author, and
   heals Home Assistant with no change in `annotatedyaml` at all.
2. **Let the type supply a copier.** An opt-in dunder on the input's class that
   probatio calls with the source and the destination.
3. **Define what the metadata _is_.** Give probatio a model of the thing being
   preserved, a single documented place values carry it, and an API validators use
   to read and add to it.

**Decision**: Take the third. `probatio.annotations` defines the model:

- `Annotations`, an immutable mapping of `str` to anything, is what a value carries.
- A value carries it in one attribute, `__probatio_annotations__`, also exported as
  `ANNOTATIONS_ATTR`. A `__slots__` type opts in with one line, a type with an
  ordinary `__dict__` needs no declaration, and a type that already keeps the
  metadata elsewhere can expose a property of that name over it. probatio only ever
  reads and writes the attribute, so all three work.
- Every site where probatio rebuilds a value carries the annotations across: the
  mapping engine, the sequence engine, `ExactSequence`, and `Object`.
- Validators read with `annotations_of`, add with `annotate`, and move annotations
  onto a value they built themselves with `carry_annotations`.

**Rationale**:

- **Carrying arbitrary state is a promise probatio cannot keep.** Option 1 copies
  whatever the instance happened to hold: a cached hash, a lock, a parent pointer, a
  memoized render. Duplicating those onto a second object is not obviously correct,
  and probatio has no way to tell the ones that should be shared from the ones that
  should not. A defined, narrow concept can be reasoned about; "the instance's
  state" cannot. It is also why option 1 has to stop at `Object`, where reapplying
  raw state would put the _unvalidated_ attributes back over the validated ones.
  Annotations have no such hazard, so the rule generalizes to every rebuild site,
  and "probatio rebuilt your value, so it kept your annotations" is a sentence that
  holds everywhere without an exception list.
- **A model lets validators participate.** This is what neither of the other options
  offers. Options 1 and 2 preserve what was already there; they give a validator no
  way to say anything. With a model there is an obvious answer: `annotate` merges
  into what a value carries, and because the engine propagates whatever it finds,
  an annotation a validator adds survives every rebuild above it. A validator that
  transforms a container calls `carry_annotations` and the metadata follows the
  transformation. Composition falls out: `All(schema, annotator)` and
  `All(annotator, schema)` both end with the loader's annotations and the
  validator's own.
- **It is the cheapest of the three.** One attribute read and one attribute write.
  Measured on CPython 3.14, `carry_annotations` costs 40 ns per rebuilt container
  when annotations are present and 26 ns when there are none. Reading the full
  default state through `object.__getstate__` is several times that, and allocates a
  state tuple and a dict to report it. On a 1500-entry nested config (1502
  containers) against the same code without the carry: a plain `dict` or `list`
  input is unchanged, because the generated validators take it and never reach the
  carry at all; a `dict`/`list` subclass that does not opt in costs 22 ns per
  container; one that does costs 49 ns. Inlining the helper at its four call sites
  was measured to save 12 ns of that and was rejected, because four copies of the
  semantics is a worse trade than 3% of the subclass path.
- **The attribute is the whole protocol.** No registration, no dunder method, no
  base class to inherit. That matters because a slotted mixin is impossible here:
  `class Node(dict, Mixin)` with a non-empty `__slots__` on the mixin is a layout
  conflict, so anything probatio shipped as a base class would not work for the
  `dict` and `list` subclasses this exists to serve.
- **Immutability makes sharing safe.** The rebuilt value gets the _same_
  `Annotations` object rather than a copy, which is what keeps the carry to two
  attribute operations. That is only sound because nothing can change it through
  either reference, so the guarantee is enforced rather than left to convention:
  the contents are copied into a dict no one else holds and reached only through a
  `MappingProxyType`. The one cost is that the proxy cannot be pickled, so
  `Annotations` reduces through its own constructor.

**Consequences**: Six additive public names (`ANNOTATIONS_ATTR`, `Annotations`,
`annotate`, `annotations_of`, `carry_annotations`, `supports_annotations`) and no
change to any existing signature. Points to fix in the design and the docs:

- **Opt-in.** A type that makes no room for the attribute carries nothing, and the
  common case (a plain `dict` in, a plain `dict` out) is untouched. Home Assistant
  gets the fix when `annotatedyaml` adds the slot, or exposes a property over the
  `__config_file__` and `__line__` it already has.
- **Values that cannot hold an attribute.** A plain `dict`, a `str`, an `int` cannot
  be annotated. `annotate` and `carry_annotations` return such a value unchanged
  rather than raise, so a validator can call them without knowing what it was handed;
  the cost is that a genuine mistake is quiet, which `supports_annotations` exists to
  let a caller check for.
- **Replace, not merge, on a rebuild.** The engine moves the source's annotations
  onto the rebuilt value, overwriting any the fresh instance gave itself in its own
  constructor. The rebuilt value stands in for the original, so the original's
  annotations are the right ones. A source carrying none leaves the new instance's
  own alone.
- **`Object` does not see the attribute.** `_iterate_object` skips it, so an
  annotated object's metadata is never offered to the attribute schema as a field
  (where `PREVENT_EXTRA` would reject it) and an unset slot is never read.
- **Type destruction is still out of reach.** A validator that returns
  `dict(value)`, a comprehension, or an accumulator produces a plain `dict` or
  `list`, and no engine change can heal that. `carry_annotations` is the fix, applied
  by the validator's author.
- **Errors do not read annotations yet.** Attaching the annotations of the offending
  value to the `Invalid` it raises is the obvious next step and is deliberately not
  in this change; the model is the prerequisite for it.
