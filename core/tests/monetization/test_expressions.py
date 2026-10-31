"""The JSONata reader the validators rely on: parse, references, worst-case lengths."""

import pytest

from fast_channels.core.monetization.expressions import (
    BOOLEAN_LENGTH,
    ExpressionError,
    Node,
    analyze,
    expression_body,
    max_length,
    parse,
    template_variables,
)


def test_a_value_is_either_a_constant_or_a_whole_expression() -> None:
    assert expression_body("{% session.uuid %}") == " session.uuid "
    assert expression_body("{%session.uuid%}") == "session.uuid"
    assert expression_body("https://ads.example.com/vast") is None
    assert expression_body("hello {% 'world' %}") is None  # mixing is not valid; it is a constant
    assert expression_body("{%%}") == ""
    assert expression_body("{%}") is None


def test_template_variables() -> None:
    body = '{"id":"fast-pod-{{session.avail_duration_secs}}s","sid":"{{ session.uuid }}"}'
    assert template_variables(body) == ["session.avail_duration_secs", "session.uuid"]
    assert template_variables("no variables") == []


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("'it''s'", None),  # two adjacent strings: refused below
        ("'a\\'b'", "a'b"),
        ('"tab\\tnew\\nline"', "tab\tnew\nline"),
        ("'x\\u0041y'", "xAy"),
        ("'\\uZZ'", "uZZ"),
        ("42", 42),
        ("4.5", 4.5),
        ("1e3", 1000.0),
        ("true", True),
        ("false", False),
        ("null", None),
    ],
)
def test_literals(text: str, value: object) -> None:
    if text == "'it''s'":
        with pytest.raises(ExpressionError, match="unexpected"):
            parse(text)
        return
    assert parse(text) == Node("literal", value)


def test_paths_filters_calls_and_chains() -> None:
    assert analyze("scte.segmentation_upid").reexport == "scte.segmentation_upid"
    assert analyze("$$.session.uuid").paths == {"session.uuid"}
    assert analyze("$.session.uuid").reexport == "session.uuid"
    assert analyze("`adsRequest`.headers.`x-fast-ctx`").paths == {"adsRequest.headers.x-fast-ctx"}
    ads = analyze("adsResponse.ads[durationSeconds > 10].adId")
    assert ads.paths == {"adsResponse.ads"}  # names inside a predicate are relative to the item
    assert analyze("adsResponse.ads[$$.session.uuid = 'x']").paths == {"adsResponse.ads", "session.uuid"}
    assert analyze("items[0]").paths == {"items"}
    assert analyze("items[]").paths == {"items"}
    chain = analyze("session.user_agent ~> $lowercase ~> $trim")
    assert chain.functions == {"lowercase", "trim"}
    assert chain.paths == {"session.user_agent"}
    assert analyze("$lookup(response.body, 'k').v").paths == {"response.body"}  # .v follows a computed base


def test_variables_lambdas_blocks_and_constructors() -> None:
    url = analyze(
        "($u := $string(scte.segmentation_upid); $id := $substring($u, 0, 4) = 'FST3' ? $substring($u, 4) : $u; "
        "'https://cdn/catalog/ctx/v1/' & $encodeUrlComponent($id) & '.json')"
    )
    assert url.functions == {"string", "substring", "encodeUrlComponent"}  # $u and $id are bound, not functions
    assert url.paths == {"scte.segmentation_upid"}
    mapped = analyze("$map([1..3, 5], function($v, $i) { $v * 2 + session.id })")
    assert mapped.functions == {"map"}
    assert mapped.paths == {"session.id"}  # a lambda body reads the input
    body = analyze("$string({'t': player_params.t, 'sid': session.uuid, 'n': -1, 'm': 17 % 5})")
    assert body.paths == {"player_params.t", "session.uuid"}
    assert analyze("$pad('', 5, 'c')").functions == {"pad"}
    assert analyze("$undefinedVariable").functions == {"undefinedVariable"}
    assert analyze("$ = $$").functions == set()


def test_regular_expressions_and_division() -> None:
    assert parse("/[^A-Za-z0-9_-]/i") == Node("regex", "/[^A-Za-z0-9_-]/i")
    assert parse("6 / 3") == Node("binary", "/", (Node("literal", 6), Node("literal", 3)))
    assert parse("$replace(x, /a\\/b/, '')").children[2] == Node("regex", "/a\\/b/")
    assert parse("a and /x/") == Node("binary", "and", (Node("name", "a"), Node("regex", "/x/")))
    assert parse("(a) / 2").kind == "binary"
    assert parse("x[0] / 2").kind == "binary"
    assert parse("'s' & /* a comment */ 't'").kind == "binary"


def test_conditions_and_assignments() -> None:
    assert parse("a ? 'x'") == Node("condition", children=(Node("name", "a"), Node("literal", "x")))
    assert len(parse("a ? 'x' : 'y'").children) == 3
    assert parse("$x := 1").kind == "assign"
    assert parse("()") == Node("block")
    assert parse("{}") == Node("object")
    assert parse("[]") == Node("array")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "empty expression"),
        ("   ", "empty expression"),
        ("'open", "unterminated string"),
        ("'open\\", "unterminated string"),
        ("/open", "unterminated regular expression"),
        ("/open\\/", "unterminated regular expression"),
        ("1 /* open", "unterminated comment"),
        ("`open", "unterminated name"),
        ("a § b", "unexpected character"),
        ("a ?? b", "unsupported JSONata operator '\\?\\?'"),
        ("a ?: b", "unsupported"),
        ("a ** b", "unsupported"),
        ("a@$x", "unsupported"),
        ("a#$i", "unsupported"),
        ("a^(b)", "unsupported"),
        ("a | b | c", "unsupported"),
        ("a ~ b", "unsupported"),
        ("a ! b", "unsupported"),
        ("a.*", "unexpected '\\*'"),
        ("%.x", "unexpected '%'"),
        ("a b", "unexpected 'b'"),
        ("1 := 2", "needs a variable"),
        ("function(a) { a }", "lambda parameters must be variables"),
        ("function($a $b) { $a }", "expected ','"),
        ("f(1", "expected ','"),
        ("{'a' 1}", "expected ':'"),
        ("[1, 2", "expected ','"),
        ("(1; 2", "expected ';'"),
        ("x[0", "expected '\\]'"),
        ("1 +", "unexpected 'end'"),
    ],
)
def test_unreadable_expressions_fail_closed(text: str, error: str) -> None:
    with pytest.raises(ExpressionError, match=error):
        analyze(text)


def test_literal_and_reexport() -> None:
    assert analyze("'BEHIND_LIVE_EDGE'").literal == "BEHIND_LIVE_EDGE"
    assert analyze("('x')").literal == "x"
    assert analyze("42").literal is None
    assert analyze("aws.logMode").reexport == "aws.logMode"
    assert analyze("aws.logMode & ''").reexport is None
    assert analyze("x").reads("x") == {"x"}
    assert analyze("session.uuid & player_params.t & sessions").reads("session", "player_params") == {
        "session.uuid",
        "player_params.t",
    }


@pytest.mark.parametrize(
    ("text", "bound"),
    [
        ("'BEHIND_LIVE_EDGE'", 16),
        ("1500", 4),
        ("1.5", 3),
        ("true", BOOLEAN_LENGTH),
        ("null", BOOLEAN_LENGTH),
        ("('a'; 'abc')", 3),
        ("()", None),
        ("x ? 'ab' : 'abcd'", 4),
        ("x ? 'ab'", 2),
        ("x ? 'ab' : response.body", None),
        ("'ab' & temp.known", 12),
        ("'ab' & response.body", None),
        ("response.statusCode = 200 and response.body.v = 1", BOOLEAN_LENGTH),
        ("a in ['x']", BOOLEAN_LENGTH),
        ("1 + 2", None),
        ("-1", None),
        ("$exists(x)", BOOLEAN_LENGTH),
        ("$string(x = 1)", BOOLEAN_LENGTH),
        ("$string(response.body.hdr)", None),
        ("$substring($string(response.body.hdr), 0, 1000)", 1000),
        ("$substring('abc', 0, 1000)", 3),
        ("$substring(x, 0)", None),
        ("$substring(x, 0, true)", None),
        ("$length(x)", None),
        ("(function($x) { $x })('a')", None),
        ("temp.known", 10),
        ("temp.unknown", None),
        ("$x", None),
    ],
)
def test_worst_case_lengths(text: str, bound: int | None) -> None:
    assert max_length(analyze(text).tree, {"temp.known": 10}) == bound
