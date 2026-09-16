# ADR-018: Carry a container subclass's instance state across a rebuild

**Date**: 2026-09-16
**Status**: Accepted

**Context**: Validating a mapping or a sequence builds a new container and fills it
with the validated values. The engine already rebuilds it as the input's own class,
so a `dict` or `list` subclass survives validation as that subclass, matching
voluptuous. What did not survive was the instance state: the rebuilt container is a
fresh, empty instance, so everything the original held in its `__dict__` or its
`__slots__` was silently dropped.

That is not a theoretical loss. Home Assistant loads YAML through `annotatedyaml`,
whose `NodeDictClass(dict)`, `NodeListClass(list)`, and `NodeStrClass(str)` declare
`__slots__ = ("__config_file__", "__line__")` and record where in the configuration
each node was written. Every schema rebuild wiped them, and error messages that
depend on them degraded. The verified case: `cv.deprecated()` prints "The 'old'
option near configuration.yaml:12 is deprecated" before validation, and loses the
"near ..." part after a single schema rebuild. A user gets told something is wrong
with no way to find it.

A `str` subclass was never affected, because a scalar is passed through rather than
rebuilt, so `NodeStrClass` already kept its annotation. The gap was containers only.

**Decision**: Copy the source container's own instance state onto the rebuilt
container, reading it through `object.__getstate__()`.

`object.__getstate__` is the standard state protocol that `pickle` and `copy`
already use, and it has been available on every object since CPython 3.11 (Probatio
requires 3.12, ADR-006), so it is always there. It returns `None` when there is
nothing to carry, a plain dict for an instance `__dict__`, and a
`(dict_or_None, slots_dict)` pair when `__slots__` are involved, with an unset slot
simply absent. One private helper, `_carry_subclass_state`, applies it at every
place the engine rebuilds a container as the input's own type:

- `_MappingValidator.__call__`, right after constructing the subclass instance and
  before the fill, so a subclass `__setitem__` runs with the state already in place.
  The carry reads the original input, not the plain dict that alias resolution
  produces.
- `_SequenceValidator.__call__`, after a successful `out_type(result)` or
  `out_type(*result)` rebuild.
- `ExactSequence.__call__`, which repeats the same rebuild.

Using the standard protocol, rather than a probatio-specific opt-in dunder, is the
point of the decision. A class that already pickles correctly carries correctly;
nobody has to learn about, or import, a Probatio hook to keep the state they
already model in the normal way. It also keeps the library honest about the drop-in
promise: Probatio holds no knowledge of `annotatedyaml` or of Home Assistant, and
any annotating loader gets the same behavior.

A class may override `__getstate__`, which makes it user code running outside the
validation itself. A broken override must not turn a valid value into a
non-`Invalid` exception, so any failure in the carry is swallowed and the rebuilt
container is simply left without the carried state. That is exactly the behavior
before this ADR, so the degradation is to the old, safe result rather than to a
crash.

This is a deliberate, documented deviation from voluptuous (ADR-001). voluptuous
builds `data.__class__()` and drops the state too, so nothing that passes
validation today changes its result: only attributes that used to be missing are
now present.

**Alternatives considered**:

- **A probatio-specific dunder (`__probatio_carry_state__`, or a marker base
  class).** Explicit, and it would let a class opt out. But it makes every
  annotating loader depend on Probatio to keep working, which is the opposite of a
  drop-in library, and it would leave `annotatedyaml` (and every other loader)
  broken until it adopted the hook. Rejected.
- **Copying `__dict__` and walking `__slots__` by hand.** It is what
  `object.__getstate__` does, minus the edge cases: an inherited `__slots__` across
  a class hierarchy, a class that models `__slots__` as a bare string, a class that
  wants to control what it exposes. Reimplementing a protocol that already exists
  buys nothing. Rejected.
- **Carrying nothing and documenting the loss.** The status quo. It leaves Home
  Assistant with worse error messages than it had on voluptuous plus
  `annotatedyaml`, for no gain. Rejected.
- **Carrying onto `Object(...)`'s rebuilt instance.** `_ObjectValidator` calls
  `type(data)(**validated)`, so the constructor runs with the validated attributes.
  Copying the source's raw state over that would overwrite validated values with
  the unvalidated originals, undoing the validation. Deliberately not done; that
  site is object construction, not container rebuilding.

**Consequences**:

- A `dict` or `list` subclass now comes back with its instance state intact, so an
  annotated node keeps its source file and line through any number of nested schema
  rebuilds. Home Assistant's `cv.deprecated()` message reads the same before and
  after validation.
- Nothing opts in and nothing can opt out short of overriding `__getstate__`. A
  class that does not want its state carried can return `None` from it, which is
  also how it would exclude that state from pickling.
- The plain `dict` and plain `list` paths pay nothing: the mapping engine only
  reaches the carry when it has constructed a real subclass instance, and the
  helper returns immediately for an exact built-in container. A subclass pays one
  `__getstate__` call, which returns `None` when there is no state. Measured on a
  10 500-line Home Assistant configuration (about 4500 container nodes) the whole
  carry costs roughly 0.3 ms, about 66 ns per container node.
- The compiled engine (ADR-011) is unaffected, and needs no generated code for
  this. Its mapping prologue guards `type(data) is not dict` and its inlined
  sequence guards `type(_v) is not list`, so any subclass input bails to the
  interpreted engine before the generated rebuild is reached. The generated code
  only ever builds plain containers, which have nothing to carry.
- A rebuild that degrades to a plain container is unchanged and carries nothing:
  a `Mapping` that is not a `dict` subclass, a `Coerce(dict)`, and the
  `except TypeError` fallback for a subclass whose constructor does not accept a
  single iterable. Those results were never the input's own type, so there is no
  state that belongs on them.
