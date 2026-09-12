#!/usr/bin/env python3
"""Plex's filter query language, as the client puts it on the wire.

The thing that makes this unguessable is that **the operator is part of the KEY,
not the value**. The client holds operators with a trailing `=` (`>>=`, `<=`,
`==`) and then strips that last character before appending its own separator
(Plex Web 4.147.1, `main-*.js` module 14056):

    const t = e.operator.slice(0, -1)
    r.push(`${e.field}${encodeURIComponent(t)}=${e.value}`)

So "year is after 2000" goes out as `year%3E%3E=2000`, and the server reads a
parameter **named** `year>>` whose value is `2000`. There is no `year>=2000`
anywhere on the wire. A server that iterates its query parameters looking for
names it knows therefore sees nothing it recognises and applies no constraint at
all -- which is what plexsim did, so every filter returned the whole library
while looking like it worked.

What each (type, operator) pair means was measured against two real servers; the
operator lists per type are not hard-coded here, they come from the `FieldType`
block of the captured `Meta` that plexsim already serves, so the engine can only
honour what it advertises.

Three more shapes, all measured:

  * a REPEATED key is AND (`?genre=6&genre=23` is the intersection), while a
    COMMA-separated value is OR (`?genre=6,27` is the union);
  * `push=1` / `pop=1` are parentheses and `and=1` / `or=1` are the separator
    between siblings, with AND binding tighter than OR, exactly as in SQL;
  * a date value may be relative (`-30d`), an absolute epoch, an ISO date or an
    ISO datetime. The suffixes are `s m h d w mon y` -- `m` is MINUTES, and
    `mon` is the only month form. An unsigned value means the past and a leading
    `+` means the future.
"""

import re
import time

# The operator a key's trailing punctuation means, keyed by what survives the
# client's `slice(0, -1)`. The right-hand side is the operator as the `Meta`
# FieldType block advertises it, which is also what /query/parse echoes back.
OPERATOR_SUFFIXES = [
    ("!=", "!=="),
    (">>", ">>="),
    ("<<", "<<="),
    ("!", "!="),
    ("=", "=="),
    (">", ">="),
    ("<", "<="),
    ("", "="),
]

# Grouping and ordering tokens, which are not filters. Deliberately short: a
# name in here can never be filtered on, and `title` IS an advertised field as
# well as a creation parameter -- listing it here made `?title=Sin` match the
# whole library, which is the exact failure this module exists to fix. Anything
# that is not an advertised field is ignored anyway.
STRUCTURAL = {"push", "pop", "and", "or", "sort", "type", "group", "having"}

_RELATIVE = re.compile(r"^([-+]?)(\d+)(s|m|h|d|w|mon|y|days|weeks)$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
            "mon": 2592000, "y": 31536000, "days": 86400, "weeks": 604800}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


class FilterError(Exception):
    """A query a real PMS refuses. `status` is the code it answers with."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def split_key(key):
    """`year>>` -> ("year", ">>="). Raises FilterError(400) for a malformed key.

    Measured: `?year%3E2010` -- a comparison character with no `=` separating it
    from the value, so the whole thing arrives as one parameter name -- is a 400
    on a real PMS, not an ignored parameter.
    """
    for suffix, operator in OPERATOR_SUFFIXES:
        if suffix and key.endswith(suffix):
            name = key[: -len(suffix)]
            break
    else:
        name, operator = key, "="
    if any(character in name for character in "<>!="):
        raise FilterError(400, "malformed filter key %r" % key)
    return name, operator


def as_epoch(value, now=None):
    """A date filter's value as an epoch. Raises FilterError(500) if unparseable.

    Measured: an unrecognised relative suffix is a 500 on a real PMS, and it is
    case sensitive -- `-1mo`, `-1M`, `-1min` and `-1week` all fail where `-1mon`
    and `-1w` work.
    """
    now = now if now is not None else time.time()
    value = str(value).strip()
    match = _RELATIVE.match(value)
    if match:
        sign, amount, unit = match.groups()
        seconds = int(amount) * _SECONDS[unit]
        # No sign means the past, which is the direction the UI's "in the last"
        # control means; a leading `+` is the future.
        return now + seconds if sign == "+" else now - seconds
    if _ISO_DATETIME.match(value):
        return _to_epoch(value[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    if _ISO_DATE.match(value):
        return _to_epoch(value, "%Y-%m-%d")
    try:
        return float(value)
    except ValueError:
        raise FilterError(500, "unparseable date %r" % value)


def _to_epoch(text, form):
    try:
        return time.mktime(time.strptime(text, form))
    except ValueError:
        raise FilterError(500, "unparseable date %r" % text)


def as_number(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise FilterError(500, "unparseable number %r" % value)


def compare(kind, operator, actual, wanted):
    """One (type, operator) pair, measured against two real servers.

        tag       =  is (by id OR by name)            != is not
        integer   =  is            != is not          <<= strictly <   >>= strictly >
                  <= at most      >= at least
        string    =  CONTAINS      != does not        == is exactly    !== is not exactly
                  <= begins with  >= ends with
        boolean   =  is            != is not
        date      <<= before      >>= after           >= inclusive after

    The surprise is `string` with a bare `=`: it is a substring match, not
    equality, and `==` is the exact one.
    """
    if kind == "date":
        if actual is None:
            # Measured on a large real library: a NULL date matches "is before"
            # and fails "is after" (3835 + 21 = 3856 rows either side).
            return operator in ("<<=", "<=")
        moment = as_epoch(wanted)
        # A row may hold a date as an ISO string (`originallyAvailableAt`) or as
        # an epoch (`addedAt`); both are dates to a filter.
        actual = as_epoch(actual) if not str(actual).replace(".", "").isdigit() \
            else as_number(actual)
        if operator in (">>=",):
            return actual > moment
        if operator in (">=",):
            return actual >= moment
        if operator in ("<<=",):
            return actual < moment
        if operator in ("<=",):
            return actual <= moment
        return abs(actual - moment) < 1

    if kind in ("integer", "boolean"):
        target = as_number(wanted)
        if actual is None:
            actual = 0
        actual = as_number(actual)
        if operator == ">>=":
            return actual > target
        if operator == ">=":
            return actual >= target
        if operator == "<<=":
            return actual < target
        if operator == "<=":
            return actual <= target
        if operator in ("!=", "!=="):
            return actual != target
        return actual == target

    # Strings and tags. A tag field's value may be a list of tag dicts.
    if isinstance(actual, (list, tuple)):
        values = []
        for entry in actual:
            if isinstance(entry, dict):
                # A tag matches by its id OR by its name -- measured: both
                # `?genre=6` and `?genre=Animation` return the same three rows.
                values.extend(str(v) for v in (entry.get("id"), entry.get("tag"))
                              if v is not None)
            else:
                values.append(str(entry))
        hit = str(wanted) in values
        return not hit if operator in ("!=", "!==") else hit

    text = "" if actual is None else str(actual)
    wanted = str(wanted)
    if operator == "==":
        return text == wanted
    if operator == "!==":
        return text != wanted
    if operator == "<=":
        return text.lower().startswith(wanted.lower())
    if operator == ">=":
        return text.lower().endswith(wanted.lower())
    if operator == "!=":
        return wanted.lower() not in text.lower()
    return wanted.lower() in text.lower()


def parse(pairs, field_type, structural=STRUCTURAL):
    """Ordered (key, value) pairs -> an expression tree.

    `field_type(name)` returns the field's advertised type, or None for a field
    this section does not have -- an unknown field is IGNORED (measured: 200 with
    the library unfiltered), which is why it cannot simply raise.

    The tree is `{"op": "and"|"or", "rules": [...]}` with leaves
    `{"field", "operator", "value", "type"}`. Grouping follows SQL: `and` binds
    tighter than `or`, `push`/`pop` are parentheses.
    """
    # Each level collects OR-groups of AND-lists: [[a, b], [c]] is (a and b) or c.
    stack = [[[]]]
    pending = None  # the separator seen since the last clause

    def place(node):
        groups = stack[-1]
        if pending == "or":
            groups.append([node])
        else:
            groups[-1].append(node)

    def collapse(groups):
        rules = []
        for group in groups:
            if not group:
                continue
            rules.append(group[0] if len(group) == 1
                         else {"op": "and", "rules": group})
        if not rules:
            return None
        return rules[0] if len(rules) == 1 else {"op": "or", "rules": rules}

    for key, value in pairs:
        if key.startswith("X-Plex-"):
            continue
        if key == "push":
            stack.append([[]])
            pending = None
            continue
        if key == "pop":
            if len(stack) > 1:
                group = collapse(stack.pop())
                if group is not None:
                    place(group)
            pending = None
            continue
        if key in ("and", "or"):
            pending = key
            continue
        if key in structural:
            continue
        name, operator = split_key(key)
        kind = field_type(name)
        if kind is None:
            continue
        if value == "":
            # Measured: `?year=` is a 500, not an empty result.
            raise FilterError(500, "empty value for %r" % name)
        # A comma-separated value is an OR over the same field -- except under a
        # negating operator, where it is an AND: measured, `?genre!=6,27` returns
        # the rows with NEITHER tag, so it is not(6) and not(27). Read as an OR
        # it would match everything that is missing either one, i.e. everything.
        alternatives = [v for v in value.split(",") if v != ""] or [value]
        leaves = [{"field": name, "operator": operator, "value": v, "type": kind}
                  for v in alternatives]
        joiner = "and" if operator in ("!=", "!==") else "or"
        place(leaves[0] if len(leaves) == 1 else {"op": joiner, "rules": leaves})
        pending = None

    while len(stack) > 1:
        group = collapse(stack.pop())
        if group is not None:
            place(group)
    return collapse(stack[0])


def evaluate(tree, row, value_of):
    """Test one row against a parsed tree. `value_of(field, row)` reads the row."""
    if tree is None:
        return True
    if "op" in tree:
        results = (evaluate(rule, row, value_of) for rule in tree["rules"])
        return all(results) if tree["op"] == "and" else any(results)
    actual = value_of(tree["field"], row)
    return compare(tree["type"], tree["operator"], actual, tree["value"])


def describe(tree, title_of, value_title_of):
    """A parsed tree as the `Query` document /query/parse answers with.

    Shape measured on a real PMS: a leaf is
    `{field, operator, fieldTitle, value}` plus `valueTitle` for a tag field,
    and a group is `{type: "composite", operator: "and"|"or", Rule: [...]}`.
    The operator is echoed in its FULL form, trailing `=` included.
    """
    if tree is None:
        return None
    if "op" in tree:
        return {"type": "composite", "operator": tree["op"],
                "Rule": [describe(rule, title_of, value_title_of)
                         for rule in tree["rules"]]}
    leaf = {"field": tree["field"], "operator": tree["operator"],
            "fieldTitle": title_of(tree["field"])}
    if tree["type"] == "tag":
        name = value_title_of(tree["field"], tree["value"])
        if name is not None:
            leaf["valueTitle"] = name
    leaf["value"] = tree["value"]
    return leaf
