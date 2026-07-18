from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
DEFAULT_UV_LOCK = REPOSITORY_ROOT / "uv.lock"
DEFAULT_DOCKERFILE = Path(__file__).with_name("pytest-deps.Dockerfile")
DEFAULT_PACKER = Path(__file__).with_name("pytest_dependency_pack.py")
DEFAULT_DOCKERIGNORE = DEFAULT_DOCKERFILE.with_name(
    f"{DEFAULT_DOCKERFILE.name}.dockerignore",
)


def dependency_metadata(pyproject: Path) -> dict[str, object]:
    document = tomllib.loads(pyproject.read_text())
    project = document.get("project", {})
    tool = document.get("tool", {})
    return {
        "build-system": document.get("build-system", {}),
        "dependency-groups": document.get("dependency-groups", {}),
        "project": {
            "dependencies": project.get("dependencies", []),
            "optional-dependencies": project.get("optional-dependencies", {}),
            "requires-python": project.get("requires-python"),
        },
        "tool.uv": tool.get("uv", {}),
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependency_image_key(
    *,
    pyproject: Path,
    uv_lock: Path,
    dockerfile: Path,
    dockerignore: Path,
    python_version: str,
    architecture: str,
    packer: Path = DEFAULT_PACKER,
    layout: str = "minimal-compiled",
    compression: str = "zstd:3",
) -> str:
    inputs = {
        "architecture": architecture,
        "compression": compression,
        "dependency-metadata": dependency_metadata(pyproject),
        "dockerfile-sha256": file_sha256(dockerfile),
        "dockerignore-sha256": file_sha256(dockerignore),
        "layout": layout,
        "packer-sha256": file_sha256(packer),
        "python-version": python_version,
        "schema": 3,
        "uv-lock-sha256": file_sha256(uv_lock),
    }
    encoded = json.dumps(
        inputs,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--uv-lock", type=Path, default=DEFAULT_UV_LOCK)
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    parser.add_argument("--packer", type=Path, default=DEFAULT_PACKER)
    parser.add_argument("--layout", default="minimal-compiled")
    parser.add_argument("--compression", default="zstd:3")
    parser.add_argument(
        "--dockerignore",
        type=Path,
        default=DEFAULT_DOCKERIGNORE,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        dependency_image_key(
            pyproject=args.pyproject,
            uv_lock=args.uv_lock,
            dockerfile=args.dockerfile,
            dockerignore=args.dockerignore,
            packer=args.packer,
            layout=args.layout,
            compression=args.compression,
            python_version=args.python_version,
            architecture=args.architecture,
        ),
    )


if __name__ == "__main__":
    main()
