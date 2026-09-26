"""Receipt lookup shared by strict artifacts and canonical analysis.

This resolves declared JSON pointers only; it does not infer configuration or
measurements by interpreting native JSON.
"""

import re

from .result import Result


def has_observed_evidence(result: Result, pointers: list[str]) -> bool:
    """Return whether a pointer identifies a non-null raw or namespaced receipt."""
    if type(pointers) is not list:
        raise ValueError("evidence_paths must be a list of JSON pointers")
    found = False
    for pointer in pointers:
        if (
            not isinstance(pointer, str)
            or not pointer.startswith("/")
            or re.search(r"~(?![01])", pointer)
        ):
            raise ValueError("evidence_paths must contain valid nonempty JSON pointers")
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]
        if len(tokens) < 2 or tokens[0] not in {"raw", "extensions"}:
            continue
        if tokens[0] == "extensions" and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+", tokens[1]
        ):
            continue
        value = getattr(result, tokens[0])
        for token in tokens[1:]:
            if isinstance(value, dict) and token in value:
                value = value[token]
            elif (
                isinstance(value, list)
                and re.fullmatch(r"0|[1-9][0-9]*", token)
                and int(token) < len(value)
            ):
                value = value[int(token)]
            else:
                break
        else:
            found |= value is not None
    return found
