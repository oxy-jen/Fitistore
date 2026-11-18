"""Pure catalog-contract tests; these do not connect to any database."""

from copy import deepcopy

import pytest
from sqlalchemy.dialects import postgresql

from app.services.marketing_suppressions import (
    _expected_check_definitions,
    _verify_catalog_checks,
    _verify_catalog_indexes,
    _verify_catalog_triggers,
    _verify_sequence_default,
    marketing_account_lock_statement,
    normalized_definition,
)


def test_same_named_but_altered_check_definition_is_rejected():
    expected = _expected_check_definitions()
    validated = {name: True for name in expected}
    _verify_catalog_checks(expected, validated)
    altered = dict(expected)
    altered["ck_marketing_suppression_case_source"] = "CHECK (true)"
    with pytest.raises(ValueError, match="altered"):
        _verify_catalog_checks(altered, validated)


def _trigger_rows():
    return [
        {
            "trigger_name": "trg_marketing_suppression_event_immutable",
            "tgenabled": "O", "trigger_type": 27,
            "function_name": "fity_reject_suppression_event_mutation",
            "function_schema": "public", "function_return": "trigger",
            "function_language": "plpgsql", "function_arg_count": 0,
            "trigger_arg_count": 0, "is_constraint_trigger": False,
            "is_internal": False,
            "function_source": "BEGIN RAISE EXCEPTION 'marketing suppression events are append-only'; END;",
        },
        {
            "trigger_name": "trg_marketing_suppression_event_no_truncate",
            "tgenabled": "O", "trigger_type": 34,
            "function_name": "fity_reject_suppression_event_mutation",
            "function_schema": "public", "function_return": "trigger",
            "function_language": "plpgsql", "function_arg_count": 0,
            "trigger_arg_count": 0, "is_constraint_trigger": False,
            "is_internal": False,
            "function_source": "BEGIN RAISE EXCEPTION 'marketing suppression events are append-only'; END;",
        },
    ]


def test_incorrect_trigger_operation_is_rejected():
    rows = _trigger_rows()
    rows[0]["trigger_type"] = 25  # BEFORE UPDATE only; DELETE is missing.
    with pytest.raises(ValueError, match="trigger/function"):
        _verify_catalog_triggers(rows, schema="public")


def test_disabled_trigger_is_rejected():
    rows = _trigger_rows()
    rows[1]["tgenabled"] = "D"
    with pytest.raises(ValueError, match="trigger/function"):
        _verify_catalog_triggers(rows, schema="public")


def _expected_index_rows():
    definitions = {
        "ix_marketing_suppression_case_account_detected": ("marketing_suppression_case", ("account_id", "detected_at"), False, None),
        "uq_marketing_suppression_case_active": ("marketing_suppression_case", ("account_id", "source", "reason_code"), True, "cleared_at IS NULL"),
        "ix_marketing_suppression_event_account_occurred": ("marketing_suppression_event", ("account_id", "occurred_at"), False, None),
        "ix_marketing_suppression_event_case_occurred": ("marketing_suppression_event", ("suppression_case_id", "occurred_at"), False, None),
        "uq_marketing_suppression_event_provider_id": ("marketing_suppression_event", ("provider_name", "provider_event_id"), True, "provider_event_id IS NOT NULL"),
    }
    rows = []
    for name, (table, columns, unique, predicate) in definitions.items():
        suffix = f" WHERE ({predicate})" if predicate else ""
        rows.append({
            "index_name": name,
            "indisvalid": True,
            "indisready": True,
            "indisunique": unique,
            "definition": f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON public.{table} USING btree ({', '.join(columns)}){suffix}",
            "predicate": predicate,
            "access_method": "btree",
            "has_expressions": False,
        })
    return definitions, rows


def test_invalid_index_is_rejected():
    expected, rows = _expected_index_rows()
    _verify_catalog_indexes(rows, expected, schema="public")
    altered = deepcopy(rows)
    altered[0]["indisvalid"] = False
    with pytest.raises(ValueError, match="invalid"):
        _verify_catalog_indexes(altered, expected, schema="public")


def test_missing_or_altered_sequence_default_is_rejected():
    _verify_sequence_default('"public"."marketing_suppression_case_id_seq"', "nextval('marketing_suppression_case_id_seq'::regclass)", "marketing_suppression_case")
    with pytest.raises(ValueError, match="sequence-backed"):
        _verify_sequence_default(None, None, "marketing_suppression_case")
    with pytest.raises(ValueError, match="different sequence"):
        _verify_sequence_default("public.marketing_suppression_case_id_seq", "nextval('other_id_seq'::regclass)", "marketing_suppression_case")


def test_check_normalization_matches_postgresql_deparser_form():
    deparsed = "CHECK (((source)::text = ANY ((ARRAY['admin'::character varying, 'provider'::character varying, 'legacy_unknown'::character varying])::text[])))"
    assert normalized_definition(deparsed) == _expected_check_definitions()["ck_marketing_suppression_case_source"]


def test_marketing_transition_lock_statement_compiles_to_postgresql_for_update():
    sql = str(marketing_account_lock_statement(42).compile(dialect=postgresql.dialect()))
    assert "customer_account" in sql
    assert "FOR UPDATE" in sql
