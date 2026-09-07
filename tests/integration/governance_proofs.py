from typing import Any


def original_basis() -> dict[str, Any]:
    return {
        "contract_version": "1.0.0",
        "root_artifact": {
            "artifact_id": "synthetic-basis-root-4519",
            "content": "synthetic qualification basis alpha",
            "content_sha256": "49e545a7685a82638a3d7f61992242eae8bb678b21935ac142809d08a05367cf",
            "version": "synthetic-root-v1",
            "license_id": "synthetic-license-root",
            "available_at": "2042-05-15T00:00:00Z",
            "valid_from": "2042-01-01T00:00:00Z",
            "valid_until": "2042-12-31T23:59:59Z",
        },
        "dependencies": [
            {
                "artifact_id": "synthetic-basis-dependency-4519",
                "content": "synthetic dependency basis beta",
                "content_sha256": (
                    "c9bd32db2fdd84621300cfd09625e251817c64194c413610441b22945019dd37"
                ),
                "version": "synthetic-dependency-v1",
                "license_id": "synthetic-license-dependency",
                "available_at": "2042-05-14T00:00:00Z",
                "valid_from": "2042-01-01T00:00:00Z",
                "valid_until": "2042-12-31T23:59:59Z",
            }
        ],
        "dependency_windows": [
            {
                "parent_artifact_id": "synthetic-basis-root-4519",
                "dependency_artifact_id": "synthetic-basis-dependency-4519",
                "required_from": "2042-05-01T00:00:00Z",
                "required_until": "2042-05-10T00:00:00Z",
            }
        ],
    }
