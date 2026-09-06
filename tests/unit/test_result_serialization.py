from stock_profiler.modules.decision_cases.domain import ExternalResult


def test_result_serialization_keeps_typed_schema_and_original_bytes() -> None:
    schema = ExternalResult.model_json_schema(mode="serialization")
    assert schema["properties"]["outcome_code"]["type"] == "string"
    assert schema["properties"]["key_reasons"]["type"] == "array"
    result = ExternalResult(outcome_code="SYNTHETIC", summary="Synthetic", key_reasons=("D0",))
    assert result.model_dump(mode="json") == {
        "outcome_code": "SYNTHETIC",
        "summary": "Synthetic",
        "key_reasons": ["D0"],
    }
