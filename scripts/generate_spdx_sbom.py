"""Generate an SPDX 2.3 JSON SBOM from the locked Python dependency graph."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

import yaml

from stock_profiler.foundation.versioning import APPLICATION_VERSION

ROOT = Path(__file__).resolve().parents[1]
LICENSE_ALIASES = {
    "Apache 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "BSD": "NOASSERTION",
    "ISC License": "ISC",
}


def build_timestamp() -> str:
    """Use the controlled build epoch so the SBOM timestamp is reproducible."""
    return (
        datetime.fromtimestamp(int(os.environ.get("SOURCE_DATE_EPOCH", "0")), UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def source_sha() -> str:
    """Use the build's immutable source identity in document-level SBOM metadata."""
    return os.environ.get("STOCK_PROFILER_SOURCE_SHA", "0" * 40)


def package_id(ecosystem: str, name: str, version: str) -> str:
    """Create a unique SPDX identifier from an ecosystem, name, and version."""
    parts = (ecosystem, name, version)
    normalized = "-".join(
        "".join(character if character.isalnum() else "-" for character in part) for part in parts
    )
    return "SPDXRef-Package-" + normalized


def package_record(
    *,
    ecosystem: str,
    name: str,
    version: str,
    download_location: str = "NOASSERTION",
    license_declared: str = "NOASSERTION",
) -> dict[str, object]:
    """Build one SPDX package record with a stable identity."""
    return {
        "SPDXID": package_id(ecosystem, name, version),
        "name": name,
        "versionInfo": version,
        "downloadLocation": download_location,
        "filesAnalyzed": False,
        "licenseConcluded": license_declared,
        "licenseDeclared": license_declared,
    }


def installed_python_license(name: str) -> str:
    """Read a normalized license expression from installed package metadata."""
    try:
        package_metadata = metadata(name)
    except PackageNotFoundError:
        return "NOASSERTION"
    expression = (
        package_metadata.get("License-Expression")
        or package_metadata.get("License")
        or "NOASSERTION"
    )
    return LICENSE_ALIASES.get(expression, expression)


def python_packages() -> list[dict[str, object]]:
    """Read the locked Python packages, excluding the root project duplicate."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return [
        package_record(
            ecosystem="pypi",
            name=package["name"],
            version=package["version"],
            license_declared=installed_python_license(package["name"]),
        )
        for package in sorted(lock["package"], key=lambda item: item["name"])
        if package["name"] != "stock-profiler"
    ]


def npm_name_and_version(package_spec: str) -> tuple[str, str]:
    """Split a pnpm package key at the final version delimiter."""
    name, separator, version = package_spec.split("(", maxsplit=1)[0].rpartition("@")
    if not separator or not name or not version:
        raise ValueError(f"unsupported pnpm package key: {package_spec}")
    return name, version


def frontend_license_inventory() -> dict[tuple[str, str], str]:
    """Read pnpm's immutable resolved license inventory."""
    completed = subprocess.run(
        ["corepack", "pnpm@10.17.1", "--dir", "web", "licenses", "list", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    raw_inventory = json.loads(completed.stdout)
    return {
        (str(component["name"]), str(version)): license_expression
        for license_expression, components in raw_inventory.items()
        for component in components
        for version in component["versions"]
    }


def frontend_packages() -> list[dict[str, object]]:
    """Read every resolved frontend package from the pnpm lockfile."""
    lock = yaml.safe_load((ROOT / "web" / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    package_specs = lock["packages"].keys()
    licenses = frontend_license_inventory()
    return [
        package_record(
            ecosystem="npm",
            name=name,
            version=version,
            license_declared=licenses.get((name, version), "NOASSERTION"),
        )
        for name, version in sorted(npm_name_and_version(spec) for spec in package_specs)
    ]


def container_base_packages() -> list[dict[str, object]]:
    """Record the pinned container base images used by the controlled build."""
    dockerfiles = (ROOT / "Dockerfile", ROOT / "deploy" / "gateway.Dockerfile")
    references = [
        line.split()[1]
        for dockerfile in dockerfiles
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    ]
    packages: list[dict[str, object]] = []
    for reference in sorted(references):
        name, digest = reference.split("@", maxsplit=1)
        packages.append(
            package_record(
                ecosystem="oci",
                name=name,
                version=digest,
            )
        )
    return packages


def build_payload() -> dict[str, object]:
    """Assemble the reproducible SBOM for Python, Web, and container inputs."""
    root = package_record(
        ecosystem="application",
        name="stock-profiler",
        version=APPLICATION_VERSION,
        license_declared="Apache-2.0",
    )
    packages = [root, *python_packages(), *frontend_packages(), *container_base_packages()]
    root_id = str(root["SPDXID"])
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"stock-profiler-{APPLICATION_VERSION}",
        "documentNamespace": (
            f"https://spdx.org/spdxdocs/stock-profiler-{APPLICATION_VERSION}-{source_sha()}"
        ),
        "creationInfo": {
            "created": build_timestamp(),
            "creators": ["Tool: stock-profiler/scripts/generate_spdx_sbom.py"],
        },
        "packages": packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": root_id,
            }
        ],
    }


def main() -> None:
    """Write the SBOM used alongside a controlled build artifact set."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = build_payload()
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
