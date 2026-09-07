from __future__ import annotations

from typing import Any, cast

from stock_profiler.modules.qualification.evidence import (
    alert_replay_result_digest,
    capability_version_digest,
    evidence_basis_digest,
    qualification_evidence_digest,
    requalification_registration_digest,
)


class _DigestModel:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(
        self,
        *,
        mode: str,
        exclude: set[str] | None = None,
    ) -> dict[str, object]:
        assert mode == "json"
        excluded = exclude or set()
        return {key: value for key, value in self._payload.items() if key not in excluded}


def test_governance_digest_entry_points_preserve_canonical_json_bytes() -> None:
    expected = "082fb53a33a0e94764dae6dc72baaeea2e04c988ef27e48769bd2bcd933de76b"
    payload = {"z": 2, "a": {"b": True}}
    model = cast(Any, _DigestModel(payload))
    replay = cast(
        Any,
        _DigestModel(
            {
                **payload,
                "original_information": {"ignored": True},
                "replay_result_digest": "ignored",
            }
        ),
    )
    registration = cast(Any, _DigestModel({**payload, "digest": "ignored"}))

    assert evidence_basis_digest(model) == expected
    assert capability_version_digest(model) == expected
    assert qualification_evidence_digest(model) == expected
    assert alert_replay_result_digest(replay) == expected
    assert requalification_registration_digest(registration) == expected
