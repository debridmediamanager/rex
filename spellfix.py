"""The measured SQLite spellfix1 subset used by Plex universal search.

PMS keeps one title vocabulary and one tag vocabulary.  A spelling pass asks
each vocabulary for ten words at score <= 200, then feeds those words back into
the ICU FTS table.  This module implements that pipeline in memory for a JSON
catalog; it deliberately does not invent a rebuild schedule for PMS's stale
spellfix tables.
"""

from collections import Counter
import unicodedata


SILENT, VOWEL, B, C, D, H, L, R, M, Y, DIGIT, SPACE, OTHER = range(13)
_CLASSNAME = ".ABCDHLRMY9 ?"
_MID = {}
for _ch, _class in (
        ("a", VOWEL), ("b", B), ("c", C), ("d", D), ("e", VOWEL),
        ("f", B), ("g", C), ("h", SILENT), ("i", VOWEL), ("j", C),
        ("k", C), ("l", L), ("m", M), ("n", M), ("o", VOWEL),
        ("p", B), ("q", C), ("r", R), ("s", C), ("t", D),
        ("u", VOWEL), ("v", B), ("w", B), ("x", C), ("y", VOWEL),
        ("z", C)):
    _MID[_ch] = _class
    _MID[_ch.upper()] = _class
for _ch in "0123456789":
    _MID[_ch] = DIGIT
for _ch in " \t\n\r\x0b\x0c":
    _MID[_ch] = SPACE
_MID["'"] = SILENT
_INIT = dict(_MID)
_INIT["y"] = _INIT["Y"] = Y
_INIT["'"] = OTHER


def _simple_fold(text):
    """ICU's tokenizer folds one code point at a time, without expansions."""
    out = []
    for char in str(text or ""):
        folded = char.casefold()
        out.append(folded if len(folded) == 1 else char)
    return "".join(out)


def tokens(text):
    """The ICU word-break shapes present in the measured title/tag tables.

    Letters, marks and numbers form words, an apostrophe inside a word stays in
    it, and punctuation is a token of its own.  Those are the distinctions PMS's
    stored vocabularies exercised (including ``law's``/``Law’s`` and GUID `/`).
    """
    text = str(text or "")
    result = []
    word = []

    def flush():
        if word:
            result.append(_simple_fold("".join(word)))
            word[:] = []

    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category[0] in "LNM":
            word.append(char)
            continue
        if (char in "'’" and word and index + 1 < len(text)
                and unicodedata.category(text[index + 1])[0] in "LNM"):
            word.append(char)
            continue
        flush()
        if not char.isspace():
            result.append(_simple_fold(char))
    flush()
    return result


_TRANSLIT = {
    "ß": "ss", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "Ð": "D", "ð": "d", "Þ": "Th", "þ": "th", "Ł": "L",
    "ł": "l", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d",
}


def transliterate(text):
    """spellfix1's ASCII comparison string for the catalog's text surface."""
    out = []
    for char in str(text or ""):
        if ord(char) < 128:
            out.append(char)
            continue
        if char in _TRANSLIT:
            out.append(_TRANSLIT[char])
            continue
        decomposed = "".join(
            c for c in unicodedata.normalize("NFKD", char)
            if ord(c) < 128 and not unicodedata.combining(c))
        out.append(decomposed or "?")
    return "".join(out)


def phonehash(value):
    """SQLite ext/misc/spellfix.c's ``phoneticHash`` for ASCII input."""
    value = str(value or "")
    if len(value) > 2 and value[0] in "gk" and value[1] == "n":
        value = value[1:]
    result = []
    previous = previous_non_silent = 0x77
    classes = _INIT
    index = 0
    while index < len(value):
        char = value[index]
        if index + 1 < len(value):
            if char == "w" and value[index + 1] == "r":
                index += 1
                continue
            if char == "d" and value[index + 1] in "jg":
                index += 1
                continue
            if (index + 2 < len(value) and char == "t"
                    and value[index + 1:index + 3] == "ch"):
                index += 1
                continue
        current = classes.get(char, OTHER)
        index += 1
        if current == SPACE:
            continue
        if current == OTHER and previous != DIGIT:
            continue
        classes = _MID
        if current == VOWEL and previous_non_silent in (R, L):
            continue
        if current in (R, L) and previous_non_silent == VOWEL and result:
            result.pop()
        previous = current
        if current == SILENT:
            continue
        previous_non_silent = current
        symbol = _CLASSNAME[current]
        if not result or result[-1] != symbol:
            result.append(symbol)
    return "".join(result)


def _character_class(previous, char):
    return (_MID if previous else _INIT).get(char, OTHER)


def _insert_delete_cost(previous, char, following):
    current_class = _character_class(previous, char)
    if current_class == SILENT:
        return 1
    if previous == char:
        return 10
    if current_class == VOWEL and (previous == "r" or following == "r"):
        return 20
    previous_class = _character_class(previous, previous)
    if current_class == previous_class:
        return 15 if current_class == VOWEL else 50
    return 100


def _substitute_cost(previous, source, target):
    if source == target:
        return 0
    if source.isalpha() and target.isalpha() and source.lower() == target.lower():
        return 0
    source_class = _character_class(previous, source)
    target_class = _character_class(previous, target)
    if source_class == target_class:
        return 40
    if B <= source_class <= Y and B <= target_class <= Y:
        return 75
    return 100


def edit_distance(source, target):
    """SQLite spellfix1's built-in ``editdist1`` cost."""
    prefix = ""
    index = 0
    while (index < len(source) and index < len(target)
           and source[index] == target[index]):
        prefix = source[index]
        index += 1
    source, target = source[index:], target[index:]
    if not source and not target:
        return 0
    if not source:
        total, previous = 0, prefix
        for index, char in enumerate(target):
            following = target[index + 1] if index + 1 < len(target) else ""
            total += _insert_delete_cost(previous, char, following) // 4
            previous = char
        return total
    if not target:
        total, previous = 0, prefix
        for index, char in enumerate(source):
            following = source[index + 1] if index + 1 < len(source) else ""
            total += _insert_delete_cost(previous, char, following)
            previous = char
        return total

    matrix = [0] * (len(target) + 1)
    context = [""] * (len(target) + 1)
    context[0] = prefix
    previous = prefix
    for column in range(1, len(target) + 1):
        char = target[column - 1]
        following = target[column] if column < len(target) else ""
        context[column] = char
        matrix[column] = matrix[column - 1] + _insert_delete_cost(
            previous, char, following)
        previous = char

    previous_source = prefix
    for row in range(1, len(source) + 1):
        last_source = row == len(source)
        source_char = source[row - 1]
        source_next = source[row] if row < len(source) else ""
        diagonal = matrix[0]
        matrix[0] = diagonal + _insert_delete_cost(
            previous_source, source_char, source_next)
        for column in range(1, len(target) + 1):
            target_char = target[column - 1]
            target_next = target[column] if column < len(target) else ""
            insertion = _insert_delete_cost(
                context[column - 1], target_char, target_next)
            if last_source:
                insertion //= 4
            deletion = _insert_delete_cost(context[column], source_char, target_next)
            substitution = _substitute_cost(
                context[column - 1], source_char, target_char)
            total, next_context = insertion + matrix[column - 1], target_char
            if deletion + matrix[column] < total:
                total, next_context = deletion + matrix[column], source_char
            if substitution + diagonal < total:
                # spellfix1 changes the cost here but deliberately keeps the
                # context chosen by the insertion/deletion branch.
                total = substitution + diagonal
            diagonal = matrix[column]
            matrix[column] = total
            context[column] = next_context
        previous_source = source_char
    return matrix[-1]


def _collation_key(value):
    """ICU primary/shifted equivalence used by PMS's title FTS table."""
    folded = unicodedata.normalize("NFKD", _simple_fold(value)).casefold()
    return "".join(char for char in folded
                   if not unicodedata.combining(char)
                   and unicodedata.category(char)[0] in "LN")


class Vocabulary:
    """One measured spellfix vocabulary, including its bounded candidate scan."""

    def __init__(self, inputs):
        ranks = Counter()
        for value in set(str(value) for value in inputs if value is not None):
            ranks.update(tokens(value))
        # The rebuild INSERT groups by token; rowid follows that grouped order.
        self.rows = []
        for rowid, word in enumerate(sorted(ranks), 1):
            k1 = transliterate(word).lower()
            self.rows.append({"rowid": rowid, "word": word,
                              "rank": ranks[word], "k1": k1,
                              "k2": phonehash(k1)})
        self.rows.sort(key=lambda row: (row["k2"], row["rowid"]))

    def candidates(self, query, top=10, cutoff=200):
        pattern = transliterate(query)
        hashed = phonehash(pattern)
        scope = 3
        if len(hashed) <= scope:
            scope = len(hashed) - 1 if len(hashed) > 2 else len(hashed)
        prefix = hashed[:scope]
        selected = []
        worst_score = 0
        worst_index = -1
        for row in self.rows:
            if not row["k2"].startswith(prefix):
                continue
            distance = edit_distance(pattern, row["k1"])
            score = distance + 32 - int(row["rank"]).bit_length()
            if score > cutoff:
                continue
            if len(selected) < top:
                index = len(selected)
            elif score < worst_score:
                index = worst_index
            else:
                continue
            candidate = dict(row, distance=distance, score=score)
            if index == len(selected):
                selected.append(candidate)
            else:
                selected[index] = candidate
            if len(selected) == top:
                worst_index = 0
                worst_score = selected[0]["score"]
                for candidate_index in range(1, len(selected)):
                    if worst_score < selected[candidate_index]["score"]:
                        worst_score = selected[candidate_index]["score"]
                        worst_index = candidate_index
        return sorted(selected, key=lambda row: row["score"])

    def groups(self, query):
        groups = []
        for original in str(query or "").split(" "):
            if len(original.encode("utf-8")) < 3:
                continue
            candidates = {_collation_key(row["word"])
                          for row in self.candidates(original)}
            groups.append((candidates, _collation_key(original)))
        return groups

    def matches(self, text, query):
        groups = self.groups(query)
        if not groups:
            return False
        haystack = tokens(text)
        return all(any(_collation_key(word) in candidates
                       or _collation_key(word).startswith(prefix)
                       for word in haystack)
                   for candidates, prefix in groups)


class SearchIndex:
    """The title and visible-tag spellfix tables captured at catalog load."""

    def __init__(self, rows, tag_fields):
        rows = list(rows)
        title_inputs = []
        tag_inputs = []
        for row in rows:
            title = row.get("title")
            if title is not None:
                # PMS stores a title_sort column even where the API omits an equal
                # titleSort attribute, so the title appears twice in this input.
                title_inputs.append("%s %s" %
                                    (title, row.get("titleSort") or title))
            for field in tag_fields:
                tag_inputs.extend(tag.get("tag") for tag in row.get(field) or [])
        self.metadata = Vocabulary(title_inputs)
        self.tags = Vocabulary(tag_inputs)

    @staticmethod
    def metadata_text(row):
        return " ".join(str(row.get(field) or "")
                        for field in ("title", "titleSort", "originalTitle"))

    def metadata_matches(self, row, query):
        return self.metadata.matches(self.metadata_text(row), query)

    def tag_matches(self, name, query):
        return self.tags.matches(name, query)
