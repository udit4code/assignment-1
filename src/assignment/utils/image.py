"""Build a task's testbed image on Modal from a local checkout.

The repository under test is copied in from `task.source` — normally the
`chess_app/` submodule — rather than cloned inside the build. That keeps the
build offline, works with a private repository without any credential, and
avoids a network round trip on every rebuild.

The cost is that the image reflects whatever is on disk, so `verify_source`
checks the `chess_app` source before every build to confirm it matches the
source the assignment intends people to fix. A testbed silently built from an
already-fixed working tree would make a broken agent look like it passed.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import modal

from assignment.task import Task

logger = logging.getLogger(__name__)

# Local caches, credentials, and git metadata never belong in the testbed. The
# .git directory in particular is a gitlink file in a submodule checkout and
# would be meaningless inside the container; the build makes a fresh repository
# instead.
IGNORED_NAMES = {".git", ".venv", ".pytest_cache", "__pycache__", ".env", ".DS_Store"}

class SourceMismatch(Exception):
    """The local checkout is not the commit the task is defined against."""

def _git(source: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True)

def is_ignored(path: Path) -> bool:
    """Whether a file should be kept out of the build context."""
    return any(part in IGNORED_NAMES for part in path.parts) or path.suffix == ".pyc"

def verify_source(task: Task, strict: bool = True) -> None:
    """Check that the local checkout matches the task's base commit.

    Args:
        task: The task whose `source` to check.
        strict: Raise on a mismatch. When False, mismatches are logged as
            warnings and the build proceeds — useful when deliberately testing
            a modified checkout.

    Raises:
        SourceMismatch: The checkout is missing, on the wrong commit, or dirty.
    """

    def complain(message: str) -> None:
        if strict:
            raise SourceMismatch(message)
        logger.warning("%s (continuing: strict=False)", message)

    if not task.source.is_dir():
        raise SourceMismatch(
            f"{task.source} does not exist. If it is a submodule, run `git submodule update --init`."
        )

    top_level = _git(task.source, "rev-parse", "--show-toplevel")
    if (
        top_level.returncode != 0
        or Path(top_level.stdout.strip()).resolve() != task.source.resolve()
    ):
        complain(
            f"{task.source} is not an initialized git checkout. "
            "Run `git submodule update --init`."
        )
        return

    head = _git(task.source, "rev-parse", "HEAD")
    if head.returncode != 0:
        complain(f"{task.source} is not a git checkout: {head.stderr.strip()}")
        return

    if head.stdout.strip() != task.base_commit:
        complain(
            f"{task.source} is at {head.stdout.strip()[:12]}, but {task.id} is defined against "
            f"{task.base_commit[:12]}. The testbed would not be the task's base commit."
        )

    dirty = _git(task.source, "status", "--porcelain")
    if dirty.stdout.strip():
        # Each line is a status code then the path. The code's width varies, so
        # split on whitespace rather than slicing at a fixed offset.
        paths = [line.strip().split(None, 1)[-1] for line in dirty.stdout.strip().splitlines()[:5]]
        changed = ", ".join(paths)
        complain(
            f"{task.source} has uncommitted changes ({changed}). The testbed would contain them, "
            "which silently invalidates the evaluation if one of them is the fix."
        )

def build_testbed_image(task: Task, strict: bool = True, force_build: bool = False) -> modal.Image:
    """Build the image holding the repository under test at its base commit.

    Args:
        task: The task whose Dockerfile and source checkout to build from.
        strict: Refuse to build when the checkout does not match the task's
            base commit. See `verify_source`.
        force_build: Skip Modal's build cache.

    Returns:
        A Modal image with the repository installed at /testbed.
    """
    verify_source(task, strict=strict)

    logger.info("Building %s from %s at %s", task.id, task.source, task.base_commit[:12])
    image = modal.Image.from_dockerfile(
        str(task.dockerfile),
        context_dir=str(task.source),
        force_build=force_build,
        ignore=is_ignored,
    )

    # Pin last, deliberately. Modal appends its own dependency install after the
    # Dockerfile's commands on image builder versions <= 2024.10, and the 2023.12
    # requirements file pins fastapi==0.88.0, which pulls starlette down to 0.22
    # and breaks every test using TestClient. Chaining here puts this layer on
    # top of that one, so the task's versions are the ones that survive.
    if task.pins:
        logger.info("Pinning %d packages on top of the built image", len(task.pins))
        image = image.pip_install(*task.pins)

    return image


def build_local_testbed_image(
    task: Task,
    strict: bool = True,
    force_build: bool = False,
    container_runtime: str = "docker",
    platform: str | None = None,
) -> str:
    """Build the task testbed with the active local Docker-compatible runtime.

    The temporary build context applies the same exclusions as the Modal image
    builder, and a derived layer installs the task's dependency pins last.
    """

    verify_source(task, strict=strict)
    safe_id = re.sub(r"[^a-z0-9_.-]+", "-", task.id.lower()).strip("-._")
    if not safe_id:
        raise ValueError(f"Task id cannot form a Docker image name: {task.id!r}")
    tag = f"assignment/{safe_id}:{task.base_commit[:12]}"
    base_tag = f"assignment/{safe_id}-base:{task.base_commit[:12]}"

    with tempfile.TemporaryDirectory(prefix="assignment-build-") as temporary:
        context = Path(temporary) / "context"
        shutil.copytree(
            task.source,
            context,
            ignore=lambda directory, names: {
                name
                for name in names
                if is_ignored(Path(directory, name).relative_to(task.source))
            },
        )
        build_command = [container_runtime, "build"]
        if force_build:
            build_command.append("--no-cache")
        if platform:
            build_command.extend(["--platform", platform])
        build_command.extend(
            ["--file", str(task.dockerfile), "--tag", base_tag, str(context)]
        )
        _run_container_build(build_command, f"base image for {task.id}")

    if not task.pins:
        tagged = subprocess.run(
            [container_runtime, "tag", base_tag, tag],
            capture_output=True,
            text=True,
        )
        if tagged.returncode != 0:
            raise RuntimeError(f"Could not tag local image: {tagged.stderr.strip()}")
        return tag

    pins = " ".join(shlex.quote(pin) for pin in task.pins)
    dockerfile = (
        f"FROM {base_tag}\n"
        f"RUN python -m pip install --no-cache-dir {pins}\n"
    )
    derived_command = [container_runtime, "build"]
    if force_build:
        derived_command.append("--no-cache")
    if platform:
        derived_command.extend(["--platform", platform])
    derived_command.extend(["--tag", tag, "-"])
    _run_container_build(
        derived_command,
        f"pinned image for {task.id}",
        input_text=dockerfile,
    )
    return tag


def _run_container_build(
    command: list[str], description: str, input_text: str | None = None
) -> None:
    logger.info("Building local %s", description)
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not run container builder: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Could not build {description}:\n{output}")
