from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
import typer


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


def main(
    *,
    architecture: str,
    python_version: str = "3.13",
    pyproject: Path = DEFAULT_PYPROJECT,
    uv_lock: Path = DEFAULT_UV_LOCK,
    dockerfile: Path = DEFAULT_DOCKERFILE,
    packer: Path = DEFAULT_PACKER,
    layout: str = "minimal-compiled",
    compression: str = "zstd:3",
    dockerignore: Path = DEFAULT_DOCKERIGNORE,
) -> None:
    print(
        dependency_image_key(
            pyproject=pyproject,
            uv_lock=uv_lock,
            dockerfile=dockerfile,
            dockerignore=dockerignore,
            packer=packer,
            layout=layout,
            compression=compression,
            python_version=python_version,
            architecture=architecture,
        ),
    )


def _cli(
    architecture: str = typer.Option(..., "--architecture"),
    python_version: str = typer.Option("3.13", "--python-version"),
    pyproject: Path = typer.Option(DEFAULT_PYPROJECT, "--pyproject"),
    uv_lock: Path = typer.Option(DEFAULT_UV_LOCK, "--uv-lock"),
    dockerfile: Path = typer.Option(DEFAULT_DOCKERFILE, "--dockerfile"),
    packer: Path = typer.Option(DEFAULT_PACKER, "--packer"),
    layout: str = typer.Option("minimal-compiled", "--layout"),
    compression: str = typer.Option("zstd:3", "--compression"),
    dockerignore: Path = typer.Option(DEFAULT_DOCKERIGNORE, "--dockerignore"),
) -> None:
    main(
        architecture=architecture,
        python_version=python_version,
        pyproject=pyproject,
        uv_lock=uv_lock,
        dockerfile=dockerfile,
        packer=packer,
        layout=layout,
        compression=compression,
        dockerignore=dockerignore,
    )


if __name__ == "__main__":
    typer.run(_cli)
