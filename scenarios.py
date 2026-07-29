"""Scenario inheritance: resolve an `extends` chain into one flat scenario.

`all_scenarios.json` holds many named scenarios under a top-level "scenarios"
map. A scenario may name a parent with "extends"; the parent may itself extend
another, to any depth. Resolving walks root-to-leaf and deep-merges each
level's fields onto the accumulated result, so a child states only what it
changes.

    {
      "scenarios": {
        "base":        { "start": "2026-01-01", "years": 1,
                         "accounts": [ {"type":"asset","name":"Assets:Checking",
                                        "opening":"100000.00","apr":"0.03"} ] },
        "higher_rate": { "extends": "base",
                         "accounts": [ {"name":"Assets:Checking","apr":"0.05"} ] },
        "long_high":   { "extends": "higher_rate", "years": 5 }
      }
    }

Merge rules (child onto parent):
  * dict + dict              -> merged key-by-key, recursively; new keys added.
  * list-of-named-dicts      -> merged BY "name": a child entry with a matching
                                name overrides that entry field-by-field; a new
                                name is appended; {"name": X, "_remove": true}
                                drops an inherited entry. (This is what lets you
                                retune one account's apr without restating the
                                whole list.)
  * anything else (scalars)  -> child replaces parent.

This module is pure data transformation — it knows nothing about the ledger.
"""

from __future__ import annotations

EXTENDS = "extends"
REMOVE = "_remove"


class ScenarioError(ValueError):
    """Missing scenario, missing parent, or a cyclic extends chain."""


def _is_named_list(x) -> bool:
    return (isinstance(x, list) and len(x) > 0
            and all(isinstance(i, dict) and "name" in i for i in x))


def _fresh(value):
    """A child-supplied value with no parent to merge onto: strip _remove markers
    from named lists so a directive never leaks in as literal data."""
    if _is_named_list(value):
        return [dict(i) for i in value if not i.get(REMOVE)]
    return value


def deep_merge(base, override):
    """Merge ``override`` onto ``base`` per the rules above. Inputs untouched."""
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, val in override.items():
            if key in result:
                result[key] = deep_merge(result[key], val)
            else:
                result[key] = _fresh(val)
        return result

    if _is_named_list(base) and isinstance(override, list):
        merged = {i["name"]: dict(i) for i in base}   # preserves base order
        for item in override:
            name = item.get("name")
            if item.get(REMOVE) is True:
                merged.pop(name, None)
                continue
            patch = {k: v for k, v in item.items() if k != REMOVE}
            if name in merged:
                merged[name] = deep_merge(merged[name], patch)
            else:
                merged[name] = patch
        return list(merged.values())

    return override


def resolve(scenarios: dict, name: str, _seen: tuple = ()) -> dict:
    """Return the fully merged, flat scenario dict for ``name``.

    ``scenarios`` is the map under the file's top-level "scenarios" key.
    """
    if name in _seen:
        chain = " -> ".join((*_seen, name))
        raise ScenarioError(f"cyclic extends chain: {chain}")
    if name not in scenarios:
        raise ScenarioError(
            f"unknown scenario {name!r}; available: {sorted(scenarios)}"
        )

    spec = scenarios[name]
    parent = spec.get(EXTENDS)
    base = resolve(scenarios, parent, _seen + (name,)) if parent else {}
    child = {k: v for k, v in spec.items() if k != EXTENDS}
    return deep_merge(base, child)


def names(root: dict) -> list[str]:
    """List the scenario names declared in a parsed all_scenarios root."""
    return sorted(root.get("scenarios", {}))