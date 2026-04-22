"""
core/oslc_utils.py — Safe helpers for building OSLC WHERE clauses.

OSLC string literals use double-quote delimiters:
    siteid="BEDFORD"
A malicious value like:  BEDFORD" or "1"="1
would produce:           siteid="BEDFORD" or "1"="1"   ← injection

oslc_escape() neutralises this by escaping backslashes and double-quotes
before the value is interpolated into the clause.

Usage:
    from core.oslc_utils import oslc_escape, safe_field_name

    where = f'siteid="{oslc_escape(site_id)}"'
"""

import re
from typing import Optional

# Only allow identifiers that look like real Maximo field names.
# Prevents injecting arbitrary OSLC syntax via a caller-supplied field name.
_SAFE_FIELD_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_.]*$')


def oslc_escape(value: str) -> str:
    """
    Escape a string for safe inclusion inside OSLC double-quoted literals.

    Escapes:
      \\  →  \\\\   (backslash must be escaped first)
      "   →  \\"    (prevents breaking out of the string delimiter)

    Args:
        value: The raw user-supplied string value.

    Returns:
        Escaped string safe for use inside  field="<value>"  clauses.
    """
    # Escape backslash first, then double-quote.
    return value.replace("\\", "\\\\").replace('"', '\\"')


def safe_field_name(name: str) -> str:
    """
    Validate that a field name only contains safe identifier characters.

    Raises ValueError if the name contains characters that could be used
    to inject OSLC syntax (e.g. spaces, quotes, operators).

    Args:
        name: The field name to validate.

    Returns:
        The original name unchanged if it is safe.

    Raises:
        ValueError: If the name fails the safety check.
    """
    if not _SAFE_FIELD_RE.match(name):
        raise ValueError(
            f"Unsafe field name rejected: {name!r}. "
            "Field names must match ^[a-zA-Z_][a-zA-Z0-9_.]*$"
        )
    return name


# ── LLM-output validators (used by tools.ai_intelligence.nl_to_oslc_query) ──
#
# Pattern-parsed clauses are already safe because the parser interpolates
# every value through oslc_escape() and uses fixed field names. The LLM
# enhancement path is the trust boundary: the model returns three free-text
# strings (where / select / orderBy) that would otherwise flow straight to
# the OSLC client. The validators below tokenize each clause against a strict
# whitelist and fail closed — callers should fall back to the pattern result
# on ValueError.

_OSLC_FORBIDDEN_TOKENS = (";", "--", "/*", "*/", "\x00")
_OSLC_KEYWORDS = frozenset({"and", "or", "not", "in", "true", "false", "null"})

_OSLC_TOKEN_RE = re.compile(
    r'''
    (?P<WS>\s+)
  | (?P<STR>"(?:\\.|[^"\\])*")
  | (?P<NUM>-?\d+(?:\.\d+)?)
  | (?P<IDENT>[a-zA-Z_][a-zA-Z0-9_.]*)
  | (?P<OP>!=|<=|>=|=|<|>)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<LBRACK>\[)
  | (?P<RBRACK>\])
  | (?P<COMMA>,)
    ''',
    re.VERBOSE,
)


def _check_forbidden(clause: str, what: str) -> None:
    for bad in _OSLC_FORBIDDEN_TOKENS:
        if bad in clause:
            raise ValueError(
                f"OSLC {what} clause contains forbidden token {bad!r}"
            )


def validate_oslc_where(clause: str, max_length: int = 1000) -> str:
    """
    Tokenize and validate an OSLC where clause produced by an LLM.

    Accepts: identifiers (matching safe_field_name), operators
    (= != < > <= >=), keywords (and/or/not/in/true/false/null), numbers,
    properly-quoted string literals, parentheses and brackets (balanced).

    Returns the clause unchanged if safe; raises ValueError otherwise.
    Empty input returns empty.
    """
    if not clause:
        return ""
    if len(clause) > max_length:
        raise ValueError(
            f"OSLC where clause too long ({len(clause)} > {max_length})"
        )
    _check_forbidden(clause, "where")

    pos = 0
    n = len(clause)
    paren_depth = 0
    bracket_depth = 0
    # Structural guard: every comparison operator must be preceded by a
    # *field* identifier — never by a literal. Blocks tautologies like
    # `1=1` or `"x"="x"` that the LLM might inject to neutralize a filter.
    last_kind: Optional[str] = None
    last_was_field_ident: bool = False
    while pos < n:
        m = _OSLC_TOKEN_RE.match(clause, pos)
        if not m:
            raise ValueError(
                f"Unrecognized character in OSLC where clause at position "
                f"{pos}: {clause[pos]!r}"
            )
        kind = m.lastgroup
        text = m.group()
        pos = m.end()
        if kind == "WS":
            continue
        if kind == "IDENT":
            lower = text.lower()
            if lower in _OSLC_KEYWORDS:
                last_was_field_ident = False
            else:
                safe_field_name(text)
                last_was_field_ident = True
        elif kind == "OP":
            if not last_was_field_ident:
                raise ValueError(
                    f"OSLC operator {text!r} must be preceded by a field "
                    f"name (not {last_kind})."
                )
            last_was_field_ident = False
        elif kind == "LPAREN":
            paren_depth += 1
            last_was_field_ident = False
        elif kind == "RPAREN":
            paren_depth -= 1
            if paren_depth < 0:
                raise ValueError("Unbalanced ')' in OSLC where clause")
            last_was_field_ident = False
        elif kind == "LBRACK":
            bracket_depth += 1
            last_was_field_ident = False
        elif kind == "RBRACK":
            bracket_depth -= 1
            if bracket_depth < 0:
                raise ValueError("Unbalanced ']' in OSLC where clause")
            last_was_field_ident = False
        else:  # STR, NUM, COMMA
            last_was_field_ident = False
        last_kind = kind
    if paren_depth != 0:
        raise ValueError("Unbalanced parentheses in OSLC where clause")
    if bracket_depth != 0:
        raise ValueError("Unbalanced brackets in OSLC where clause")
    return clause


def validate_oslc_select(clause: str, max_length: int = 500) -> str:
    """
    Validate a comma-separated OSLC select clause (or '*'). Each comma-split
    field must pass safe_field_name. Returns the clause unchanged if safe.
    """
    if not clause or clause.strip() == "*":
        return clause
    if len(clause) > max_length:
        raise ValueError(
            f"OSLC select clause too long ({len(clause)} > {max_length})"
        )
    _check_forbidden(clause, "select")
    for raw in clause.split(","):
        part = raw.strip()
        if not part:
            raise ValueError("Empty field name in OSLC select clause")
        if part == "*":
            continue
        safe_field_name(part)
    return clause


def validate_oslc_orderby(clause: str, max_length: int = 200) -> str:
    """
    Validate a single-field OSLC orderBy clause, optionally prefixed with
    '+' (ascending) or '-' (descending). Returns the clause unchanged if safe.
    """
    if not clause:
        return clause
    if len(clause) > max_length:
        raise ValueError(
            f"OSLC orderBy clause too long ({len(clause)} > {max_length})"
        )
    # orderBy is a single field; commas would silently allow extra fields.
    for bad in (*_OSLC_FORBIDDEN_TOKENS, ","):
        if bad in clause:
            raise ValueError(
                f"OSLC orderBy clause contains forbidden token {bad!r}"
            )
    name = clause.strip()
    if name.startswith(("+", "-")):
        name = name[1:]
    safe_field_name(name)
    return clause
