"""Narrow, lossless duplicate normalization for Brownfield tool arguments only.

No fence stripping, coercion, value selection, schema repair or provider retry.
Other malformed input passes unchanged to the existing strict diagnostic parser.
"""
import json
import math

POLICY = 'identical-typed-duplicates/1'


class CollectorConflict(ValueError):
    def __init__(self):
        super().__init__('Conflicting collector fields')


def _identical(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_identical(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right))
    return left == right


def normalize_arguments(raw):
    """Return (JSON text, duplicates removed); never include fields in errors."""
    count = 0

    def pairs(items):
        nonlocal count
        result = {}
        for key, value in items:
            if key in result:
                if not _identical(result[key], value):
                    raise CollectorConflict()
                count += 1
            else:
                result[key] = value
        return result

    def reject_constant(_):
        raise ValueError('Nonfinite JSON')

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('Nonfinite JSON')
        return value

    try:
        raw.encode('utf-8')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant, parse_float=finite_float)
        if not count:
            return raw, 0
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        text.encode('utf-8')
        return text, count
    except CollectorConflict:
        raise
    except (ValueError, UnicodeError, RecursionError):
        return raw, 0
