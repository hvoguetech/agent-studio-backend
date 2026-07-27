"""`$each` loop support in JSON body templates (batch many rows in one call).

Before this, one REST tool call could only build a fixed-shape body, so an agent editing N rows
made N calls (slow, context-heavy, and prone to blowing the graph recursion limit). A body
template can now carry a `{"$each": "{{input.rows}}", "$as": "row", "$do": {...}}` directive that
expands one list-valued arg into a variable-length JSON array - so N edits go out in ONE request.

Rendering is structural (parse JSON, then walk with render_value): the output is always valid
JSON with native types preserved, unlike string-concatenating an array. The path is opt-in on the
"$each" marker, so existing string-substitution templates are untouched.
"""
from __future__ import annotations

import json

from forge.auth_providers.templates import has_each_directive, render_template, render_value
from forge.tools.rest import _build_body


def test_render_value_each_expands_per_item():
    tmpl = {
        "$each": "{{input.rows}}",
        "$as": "row",
        "$do": {"col": "{{row.editedCol}}", "val": "{{row.editedValue}}"},
    }
    rows = [
        {"editedCol": "unitCost", "editedValue": "96"},
        {"editedCol": "unitCost", "editedValue": "33"},
    ]
    out = render_value(tmpl, {"input": {"rows": rows}}, allow_each=True)
    assert out == [
        {"col": "unitCost", "val": "96"},
        {"col": "unitCost", "val": "33"},
    ]


def test_render_value_each_preserves_native_types_and_outer_vars():
    tmpl = {
        "orderId": "{{input.orderId}}",
        "rows": {"$each": "{{input.rows}}", "$as": "r", "$do": {"n": "{{r.n}}"}},
    }
    out = render_value(tmpl, {"input": {"orderId": "ORD-001", "rows": [{"n": 1}, {"n": 2}]}}, allow_each=True)
    # line numbers stay ints (whole-string token preserves native type); orderId interpolated.
    assert out == {"orderId": "ORD-001", "rows": [{"n": 1}, {"n": 2}]}


def test_render_value_each_missing_yields_empty_list():
    tmpl = {"$each": "{{input.rows}}", "$as": "row", "$do": {"x": "{{row.x}}"}}
    assert render_value(tmpl, {"input": {}}, allow_each=True) == []


def test_render_value_each_single_value_treated_as_one_item():
    tmpl = {"$each": "{{input.row}}", "$as": "row", "$do": {"x": "{{row.x}}"}}
    assert render_value(tmpl, {"input": {"row": {"x": "only"}}}, allow_each=True) == [{"x": "only"}]


def test_render_value_each_is_opt_in_only():
    # WITHOUT allow_each (the default for auth token_fetch / data-node payload callers), a dict that
    # happens to have a "$each" KEY must stay an ordinary object, NOT be reinterpreted as a loop.
    tmpl = {"$each": "{{input.rows}}", "$as": "row", "$do": {"x": "{{row.x}}"}}
    out = render_value(tmpl, {"input": {"rows": [{"x": "a"}]}})
    assert out == {"$each": [{"x": "a"}], "$as": "row", "$do": {"x": None}}


def test_has_each_directive_ignores_literal_string_value():
    # A directive is a "$each" KEY; the literal text "$each" inside a string value is not one.
    assert has_each_directive({"note": "use $each to loop", "qty": "{{input.qty}}"}) is False
    assert has_each_directive({"rows": {"$each": "{{x}}", "$do": {}}}) is True


def test_build_body_literal_dollar_each_in_string_keeps_string_substitution():
    # A valid-JSON template that merely MENTIONS "$each" in a value must not switch to structural
    # rendering (which would change token type coercion). The quoted token stays a string "5".
    body_template = json.dumps({"qty": "{{input.qty}}", "note": "$each is a keyword"})
    body = _build_body({"body_template": body_template}, [], {"qty": 5}, {})
    assert body == {"qty": "5", "note": "$each is a keyword"}


def test_render_template_embedded_falsy_values_are_not_dropped():
    # A falsy-but-real value embedded in a larger string must render literally (0 -> "0"), not be
    # swallowed to "" - which is what a `_lookup(...) or ""` would wrongly do.
    assert render_template("qty={{input.qty}}", {"input": {"qty": 0}}) == "qty=0"
    assert render_template("on={{input.flag}}", {"input": {"flag": False}}) == "on=False"
    assert render_template("x={{input.missing}}", {"input": {}}) == "x="


def test_build_body_batches_multiple_rows_into_one_body():
    body_template = json.dumps({
        "orderId": "{{input.orderId}}",
        "items": {
            "$each": "{{input.rows}}",
            "$as": "row",
            "$do": {
                "editedCol": "{{row.editedCol}}",
                "editedValue": "{{row.editedValue}}",
                "applyConversion": True,
                "lineNums": ["{{row.lineNum}}"],
                "enforcePolicy": False,
            },
        },
    })
    values = {
        "orderId": "ORD-001",
        "rows": [
            {"editedCol": "unitCost", "editedValue": "96", "lineNum": 1},
            {"editedCol": "unitCost", "editedValue": "33", "lineNum": 2},
        ],
    }
    body = _build_body({"body_template": body_template}, [], values, {})
    assert body["orderId"] == "ORD-001"
    assert len(body["items"]) == 2
    assert body["items"][0] == {
        "editedCol": "unitCost", "editedValue": "96",
        "applyConversion": True, "lineNums": [1], "enforcePolicy": False,
    }
    assert body["items"][1]["lineNums"] == [2]


def test_build_body_passthrough_rows_and_injects_constants():
    # Mirrors the live agent input: the model sends items items carrying an lineNums
    # ARRAY and no constants; the template passes each row's fields through (preserving the array)
    # and injects the fixed applyConversion/enforcePolicy server-side.
    body_template = json.dumps({
        "orderId": "{{input.orderId}}",
        "items": {
            "$each": "{{input.items}}",
            "$as": "row",
            "$do": {
                "editedCol": "{{row.editedCol}}",
                "editedValue": "{{row.editedValue}}",
                "applyConversion": True,
                "lineNums": "{{row.lineNums}}",
                "enforcePolicy": False,
            },
        },
    })
    values = {
        "orderId": "ORD-001",
        "items": [
            {"editedCol": "unitCost", "editedValue": "777", "lineNums": [1]},
            {"editedCol": "unitCost", "editedValue": "777", "lineNums": [2]},
        ],
    }
    body = _build_body({"body_template": body_template}, [], values, {})
    assert body["orderId"] == "ORD-001"
    assert body["items"] == [
        {"editedCol": "unitCost", "editedValue": "777", "applyConversion": True,
         "lineNums": [1], "enforcePolicy": False},
        {"editedCol": "unitCost", "editedValue": "777", "applyConversion": True,
         "lineNums": [2], "enforcePolicy": False},
    ]


def test_build_body_without_each_is_unchanged():
    # Legacy unquoted-token template (not valid JSON as text) still uses string substitution and
    # keeps producing a number for the bare {{token}}.
    body_template = '{ "orderId": "{{input.orderId}}", "lineNums": [ {{input.lineNum}} ] }'
    body = _build_body({"body_template": body_template}, [], {"orderId": "Q", "lineNum": 7}, {})
    assert body == {"orderId": "Q", "lineNums": [7]}
