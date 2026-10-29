"""Static analysis of MediaTailor Functions values and ADS templates.

A Functions value is either a constant or a whole JSONata expression ``{% … %}`` (never a mix; UG
"JSONata expression reference"). MediaTailor checks neither the documented length limits nor the function allow-list
when a function is put (P0-20 §4.5), so the validators read every expression with the parser here: the functions it
calls, the input paths it reads, and an upper bound on the length of what it can return.

The parser accepts the JSONata subset the Functions recipes use (literals, paths with predicates, variables, calls,
lambdas, blocks with ``:=``, array and object constructors, arithmetic, comparison, ``&``, ``and``/``or``, ``in``,
``~>`` and the conditional). Anything else (wildcards, parent ``%``, ``@``/``#`` bindings, order-by, transforms,
``??``/``?:``) is refused with :class:`ExpressionError`: a validator that cannot read an expression cannot prove
the rules, so it fails closed.

ADS templates (the playback configuration's ``Body`` and ``Headers``) use ``{{variable}}`` instead; see
:func:`template_variables`.
"""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Final

EXPRESSION_OPEN: Final = "{%"
EXPRESSION_CLOSE: Final = "%}"
TEMPLATE_VARIABLE: Final = re.compile(r"\{\{([^{}]*)\}\}")
"""An ADS template variable, ``{{session.uuid}}`` (MediaTailor's syntax in the POST body and header values)."""

BOOLEAN_LENGTH: Final = len("false")
"""Longest rendering of a boolean output value."""


class ExpressionError(ValueError):
    """An expression is not valid JSONata, or uses syntax outside the subset this module reads."""


def expression_body(value: str) -> str | None:
    """The JSONata text of ``value`` when it is an expression (``{% … %}``), else None (a constant)."""
    if value.startswith(EXPRESSION_OPEN) and value.endswith(EXPRESSION_CLOSE) and len(value) >= 4:
        return value[2:-2]
    return None


def template_variables(template: str) -> list[str]:
    """The ``{{variable}}`` names of an ADS template, in order, stripped of surrounding blanks."""
    return [name.strip() for name in TEMPLATE_VARIABLE.findall(template)]


# --- lexer ---------------------------------------------------------------------------------------------------------

_OPERATORS: Final = (":=", "~>", "!=", "<=", ">=", "..", "??", "?:", "**")
_PUNCTUATION: Final = frozenset(".[](){},;:?+-*/%&=<>|^#@!~")
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VARIABLE: Final = re.compile(r"\$\$|\$[A-Za-z_][A-Za-z0-9_]*|\$")
_NUMBER: Final = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_KEYWORD_OPERATORS: Final = frozenset({"and", "or", "in"})
_UNSUPPORTED: Final = frozenset({"??", "?:", "**", "@", "#", "^", "|", "!", "~"})


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # "str", "num", "name", "var", "regex", "op", "end"
    text: str
    position: int


def _string(text: str, start: int) -> tuple[str, int]:
    quote, position, chars = text[start], start + 1, []
    while position < len(text):
        char = text[position]
        if char == "\\":
            if position + 1 >= len(text):
                break
            escaped = text[position + 1]
            if escaped == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", code := text[position + 2 : position + 6]):
                chars.append(chr(int(code, 16)))
                position += 6
                continue
            chars.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(escaped, escaped))
            position += 2
            continue
        if char == quote:
            return "".join(chars), position + 1
        chars.append(char)
        position += 1
    raise ExpressionError(f"unterminated string at {start}")


def _regex(text: str, start: int) -> tuple[str, int]:
    position = start + 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == "/":
            end = position + 1
            while end < len(text) and text[end] in "im":
                end += 1
            return text[start:end], end
        position += 1
    raise ExpressionError(f"unterminated regular expression at {start}")


def _operand_ends(previous: _Token | None) -> bool:
    """Whether the token before a ``/`` ends an operand (then ``/`` divides; otherwise it starts a regex)."""
    if previous is None:
        return False
    if previous.kind in {"str", "num", "var", "regex"}:
        return True
    if previous.kind == "name":
        return previous.text not in _KEYWORD_OPERATORS
    return previous.text in {")", "]", "}"}


_PATTERNS: Final = (("var", _VARIABLE), ("num", _NUMBER), ("name", _NAME))


def _token(text: str, position: int, previous: _Token | None) -> tuple[_Token, int]:
    """The token at ``position`` and where it ends."""
    char = text[position]
    if char in "'\"":
        value, end = _string(text, position)
        return _Token("str", value, position), end
    if char == "`":
        end = text.find("`", position + 1)
        if end < 0:
            raise ExpressionError(f"unterminated name at {position}")
        return _Token("name", text[position + 1 : end], position), end + 1
    if char == "/" and not _operand_ends(previous):
        value, end = _regex(text, position)
        return _Token("regex", value, position), end
    for kind, pattern in _PATTERNS:
        if match := pattern.match(text, position):
            return _Token(kind, match.group(), position), match.end()
    operator = next((op for op in _OPERATORS if text.startswith(op, position)), char)
    if operator not in _OPERATORS and operator not in _PUNCTUATION:
        raise ExpressionError(f"unexpected character {char!r} at {position}")
    return _Token("op", operator, position), position + len(operator)


def _tokens(text: str) -> Iterator[_Token]:
    position, previous = 0, None
    while position < len(text):
        if text[position].isspace():
            position += 1
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                raise ExpressionError(f"unterminated comment at {position}")
            position = end + 2
        else:
            previous, position = _token(text, position, previous)
            yield previous
    yield _Token("end", "", len(text))


# --- parser --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Node:
    """A node of the expression tree: ``kind`` with its ``value`` and ``children``.

    Kinds: ``literal`` (value: the Python value), ``regex``, ``name`` (a path step), ``variable`` (value: the name
    without ``$``; ``""`` is the context ``$``, ``"$"`` the root ``$$``), ``path`` (children: base and step),
    ``filter`` (base, predicate), ``call`` (callee, arguments...), ``lambda`` (value: parameter names; children:
    body), ``block``, ``array``, ``range``, ``object`` (children: key, value, key, value...), ``assign`` (value:
    variable name; children: expression), ``unary`` (value: ``-``), ``binary`` (value: the operator), ``condition``
    (test, then, else?).
    """

    kind: str
    value: object = None
    children: tuple[Node, ...] = ()


_BINDING: Final = {
    ".": 75,
    "[": 80,
    "(": 80,
    "?": 20,
    "+": 50,
    "-": 50,
    "*": 60,
    "/": 60,
    "%": 60,
    "&": 50,
    "=": 40,
    "<": 40,
    ">": 40,
    "!=": 40,
    "<=": 40,
    ">=": 40,
    "in": 40,
    "~>": 40,
    "and": 30,
    "or": 25,
    ":=": 10,
}
"""Left binding powers, from the JSONata reference parser (operators this module reads)."""
_CONSTANTS: Final[dict[str, bool | None]] = {"true": True, "false": False, "null": None}


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = list(_tokens(text))
        self.index = 0

    @property
    def token(self) -> _Token:
        return self.tokens[self.index]

    def advance(self, expected: str | None = None) -> _Token:
        token = self.token
        if expected is not None and token.text != expected:
            raise ExpressionError(f"expected {expected!r} at {token.position}, found {token.text or 'the end'!r}")
        self.index += 1
        return token

    def infix_power(self) -> int:
        token = self.token
        if token.kind == "op" or (token.kind == "name" and token.text in _KEYWORD_OPERATORS):
            if token.text in _UNSUPPORTED:
                raise ExpressionError(f"unsupported JSONata operator {token.text!r} at {token.position}")
            return _BINDING.get(token.text, 0)
        return 0

    def expression(self, power: int = 0) -> Node:
        left = self.prefix(self.advance())
        while power < self.infix_power():
            left = self.infix(self.advance(), left)
        return left

    def prefix(self, token: _Token) -> Node:
        match token.kind:
            case "str":
                return Node("literal", token.text)
            case "num":
                return Node("literal", float(token.text) if any(c in token.text for c in ".eE") else int(token.text))
            case "regex":
                return Node("regex", token.text)
            case "var":
                return Node("variable", token.text[1:])
            case "name":
                return self.prefix_name(token.text)
        return self.prefix_operator(token)

    def prefix_name(self, name: str) -> Node:
        if name in _CONSTANTS:
            return Node("literal", _CONSTANTS[name])
        return self.function() if name == "function" else Node("name", name)

    def prefix_operator(self, token: _Token) -> Node:
        match token.text:
            case "-":
                return Node("unary", "-", (self.expression(70),))
            case "(":
                return Node("block", children=self.sequence(")", ";"))
            case "[":
                return Node("array", children=self.sequence("]", ",", items=True))
            case "{":
                return self.object()
        raise ExpressionError(f"unexpected {token.text or 'end'!r} at {token.position}")

    def infix(self, token: _Token, left: Node) -> Node:
        match token.text:
            case ".":
                return Node("path", children=(left, self.expression(75)))
            case "[":
                predicate = Node("block") if self.token.text == "]" else self.expression()
                self.advance("]")
                return Node("filter", children=(left, predicate))
            case "(":
                return Node("call", children=(left, *self.sequence(")", ",")))
            case "?":
                return self.condition(left)
            case ":=":
                return self.assign(token, left)
            case operator:
                return Node("binary", operator, (left, self.expression(_BINDING[operator])))

    def condition(self, test: Node) -> Node:
        then = self.expression()
        if self.token.text != ":":
            return Node("condition", children=(test, then))
        self.advance(":")
        return Node("condition", children=(test, then, self.expression()))

    def assign(self, token: _Token, variable: Node) -> Node:
        if variable.kind != "variable":
            raise ExpressionError(f"':=' needs a variable on its left at {token.position}")
        return Node("assign", variable.value, (self.expression(9),))

    def sequence(self, close: str, separator: str, *, items: bool = False) -> tuple[Node, ...]:
        nodes: list[Node] = []
        while self.token.text != close:
            node = self.expression()
            if items and self.token.text == "..":
                self.advance()
                node = Node("range", children=(node, self.expression()))
            nodes.append(node)
            if self.token.text != close:
                self.advance(separator)
        self.advance(close)
        return tuple(nodes)

    def object(self) -> Node:
        pairs: list[Node] = []
        while self.token.text != "}":
            pairs.append(self.expression())
            self.advance(":")
            pairs.append(self.expression())
            if self.token.text != "}":
                self.advance(",")
        self.advance("}")
        return Node("object", children=tuple(pairs))

    def function(self) -> Node:
        self.advance("(")
        parameters: list[str] = []
        while self.token.text != ")":
            parameter = self.advance()
            if parameter.kind != "var":
                raise ExpressionError(f"lambda parameters must be variables at {parameter.position}")
            parameters.append(parameter.text[1:])
            if self.token.text != ")":
                self.advance(",")
        self.advance(")")
        self.advance("{")
        body = self.expression()
        self.advance("}")
        return Node("lambda", tuple(parameters), (body,))


def parse(text: str) -> Node:
    """Parse JSONata ``text`` (the inside of ``{% … %}``) into a :class:`Node` tree.

    Raises:
        ExpressionError: invalid JSONata, or syntax outside the subset this module reads.
    """
    parser = _Parser(text)
    if parser.token.kind == "end":
        raise ExpressionError("empty expression")
    node = parser.expression()
    if parser.token.kind != "end":
        raise ExpressionError(f"unexpected {parser.token.text!r} at {parser.token.position}")
    return node


# --- analysis ------------------------------------------------------------------------------------------------------


def _steps(node: Node) -> tuple[object, list[str]] | None:
    """``(anchor, names)`` when ``node`` is a path of plain names: anchor None for a bare path, ``""`` after the
    context ``$``, ``"$"`` after the root ``$$``; None for any other node."""
    match node.kind:
        case "name":
            return None, [str(node.value)]
        case "variable" if node.value in {"", "$"}:
            return node.value, []
        case "path":
            base, step = node.children
            if step.kind == "name" and (inner := _steps(base)) is not None:
                return inner[0], [*inner[1], str(step.value)]
    return None


def _dotted(node: Node) -> str | None:
    """``a.b.c`` when ``node`` is a path of plain names (optionally after ``$`` or ``$$``), else None."""
    steps = _steps(node)
    return ".".join(steps[1]) if steps is not None and steps[1] else None


@dataclass(slots=True)
class _Walk:
    functions: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    bound: set[str] = field(default_factory=set)

    def visit(self, node: Node, *, relative: bool = False) -> None:
        """Collect references. Names inside a predicate, or after a computed base, are ``relative`` to an item and
        are not inputs; a path from the root ``$$`` always is."""
        if (steps := _steps(node)) is not None:
            anchor, names = steps
            if names and (anchor == "$" or not relative):
                self.paths.add(".".join(names))
            return
        match node.kind:
            case "variable" if node.value not in self.bound:
                self.functions.add(str(node.value))
            case "assign":
                self.bound.add(str(node.value))
                self.visit(node.children[0], relative=relative)
            case "lambda":
                parameters = node.value if isinstance(node.value, tuple) else ()
                self.bound.update(str(p) for p in parameters)
                self.visit(node.children[0], relative=relative)
            case "path" if node.children[1].kind == "filter" and _steps(node.children[0]) is not None:
                step, predicate = node.children[1].children  # a.b[p]: JSONata binds the predicate to the step b
                self.visit(Node("path", children=(node.children[0], step)), relative=relative)
                self.visit(predicate, relative=True)
            case "path" | "filter":
                base, step = node.children
                self.visit(base, relative=relative)
                self.visit(step, relative=True)
            case _:
                for child in node.children:
                    self.visit(child, relative=relative)


@dataclass(frozen=True, slots=True)
class Analysis:
    """What one expression references.

    Attributes:
        functions: names of the functions it calls or references (without ``$``), excluding variables it binds.
        paths: dotted input paths it reads (``scte.segmentation_upid``); a path through a predicate counts up to
            the predicate.
        literal: the string when the whole expression is one string literal, else None.
        reexport: the path when the whole expression is one plain path, else None.
    """

    tree: Node
    functions: frozenset[str]
    paths: frozenset[str]
    literal: str | None
    reexport: str | None

    def reads(self, *namespaces: str) -> set[str]:
        """The paths read under any of ``namespaces`` (``session`` matches ``session`` and ``session.*``)."""
        return {p for p in self.paths for ns in namespaces if p == ns or p.startswith(f"{ns}.")}


def analyze(text: str) -> Analysis:
    """Parse and analyse the JSONata ``text`` (the inside of ``{% … %}``).

    Raises:
        ExpressionError: as :func:`parse`.
    """
    tree = parse(text)
    walk = _Walk()
    walk.visit(tree)
    unwrapped = tree.children[0] if tree.kind == "block" and len(tree.children) == 1 else tree
    literal = unwrapped.value if unwrapped.kind == "literal" and isinstance(unwrapped.value, str) else None
    return Analysis(
        tree=tree,
        functions=frozenset(walk.functions),
        paths=frozenset(walk.paths),
        literal=literal,
        reexport=_dotted(unwrapped),
    )


_BOOLEAN_OPERATORS: Final = frozenset({"=", "!=", "<", ">", "<=", ">=", "in", "and", "or"})
_BOOLEAN_FUNCTIONS: Final = frozenset({"boolean", "not", "exists", "contains"})


def max_length(node: Node, known: Mapping[str, int]) -> int | None:
    """An upper bound on the length of the string ``node`` evaluates to, or None when none can be proved.

    ``known`` bounds the input paths written earlier in the same tree (``temp.c_hdr`` → 1,000). Literals, booleans,
    conditionals, ``&``, ``$string`` of a bounded value and ``$substring`` with a literal length are bounded; any
    other value read from a response or the session is not.
    """
    match node.kind:
        case "literal":
            return BOOLEAN_LENGTH if node.value is None or isinstance(node.value, bool) else len(_text(node.value))
        case "block" if node.children:
            return max_length(node.children[-1], known)
        case "condition":
            branches = [max_length(child, known) for child in node.children[1:]]
            return None if None in branches else max(b for b in branches if b is not None)
        case "binary":
            return _binary_length(node, known)
        case "call":
            return _call_length(node, known)
    dotted = _dotted(node)
    return None if dotted is None else known.get(dotted)


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _binary_length(node: Node, known: Mapping[str, int]) -> int | None:
    if node.value in _BOOLEAN_OPERATORS:
        return BOOLEAN_LENGTH
    if node.value != "&":
        return None
    left, right = (max_length(child, known) for child in node.children)
    return None if left is None or right is None else left + right


def _call_length(node: Node, known: Mapping[str, int]) -> int | None:
    callee, *arguments = node.children
    if callee.kind != "variable":
        return None
    match callee.value, arguments:
        case name, _ if name in _BOOLEAN_FUNCTIONS:
            return BOOLEAN_LENGTH
        case "string", [argument]:
            return max_length(argument, known)
        case "substring", [text, _, Node(kind="literal", value=int() as length)] if not isinstance(length, bool):
            bound = max_length(text, known)
            return length if bound is None else min(length, bound)
    return None
