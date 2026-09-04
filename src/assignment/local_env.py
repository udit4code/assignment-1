"""Local Docker sandbox compatible with the assignment agent environments.

Colima exposes a normal Docker context, so this class deliberately talks to the
Docker CLI rather than to Colima itself. No host directory or Docker socket is
mounted into the container.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import uuid
from pathlib import Path, PurePath
from typing import Any, Sequence


class LocalDockerEnvironment:
    """Execute commands in one disposable local Docker container."""

    def __init__(
        self,
        image: str | PurePath = "python:3.12",
        cwd: str = "/",
        startup_timeout: float = 120,
        runtime_timeout: float = 600,
        deployment_timeout: float = 600,
        conda_env: str | None = None,
        exposed_ports: Sequence[int] = (),
        container_runtime: str = "docker",
        platform: str | None = None,
        docker_args: Sequence[str] = (),
    ):
        if not isinstance(image, (str, PurePath)):
            raise TypeError("The Docker backend requires an image name.")
        if deployment_timeout <= 0:
            raise ValueError("deployment_timeout must be positive")

        self.image = str(image)
        self.cwd = cwd
        self.runtime_timeout = runtime_timeout
        self.container_runtime = container_runtime
        self.container_name = f"assignment-{uuid.uuid4().hex[:12]}"
        self.env_defaults: dict[str, str] = {}
        self._stopped = False
        self._stop_lock = threading.Lock()
        self._lifetime_timer: threading.Timer | None = None

        command = [
            container_runtime,
            "run",
            "--detach",
            "--rm",
            "--init",
            "--name",
            self.container_name,
            "--cpus",
            os.environ.get("ASSIGNMENT_DOCKER_CPUS", "2"),
            "--memory",
            os.environ.get("ASSIGNMENT_DOCKER_MEMORY", "2g"),
            "--pids-limit",
            "512",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "/bin/sh",
        ]
        if platform:
            command.extend(["--platform", platform])
        for port in exposed_ports:
            if not 1 <= port <= 65535:
                raise ValueError(f"Invalid exposed port: {port}")
            command.extend(["--publish", f"127.0.0.1::{port}"])
        command.extend(docker_args)
        command.extend(
            [
                self.image,
                "-c",
                "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
            ]
        )

        try:
            started = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=startup_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not start local Docker sandbox: {exc}") from exc
        if started.returncode != 0:
            raise RuntimeError(
                "Could not start local Docker sandbox: "
                + (started.stderr or started.stdout).strip()
            )

        try:
            if conda_env:
                self.activate_conda_env(conda_env)
            platform_result = self.execute("uname -s; uname -r; uname -v; uname -m")
            values = platform_result["output"].splitlines()
            if platform_result["returncode"] != 0 or len(values) != 4:
                raise RuntimeError(
                    "Could not read platform information from local sandbox: "
                    + platform_result["output"].strip()
                )
            self.system, self.release, self.version, self.machine = values
        except Exception:
            self.stop()
            raise

        self._lifetime_timer = threading.Timer(deployment_timeout, self.stop)
        self._lifetime_timer.daemon = True
        self._lifetime_timer.start()

    def _docker(self, *arguments: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.container_runtime, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def is_alive(self) -> bool:
        if self._stopped:
            return False
        try:
            result = self._docker(
                "inspect",
                "--format",
                "{{.State.Running}}",
                self.container_name,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def activate_conda_env(self, name: str, root: str = "/opt/miniconda3") -> str:
        binary_dir = f"{root}/envs/{name}/bin"
        if self.execute(f"test -d {shlex.quote(binary_dir)}", cwd="/")["returncode"] != 0:
            raise FileNotFoundError(f"No conda environment at {binary_dir}")
        current = self.execute("printenv PATH", cwd="/")["output"].strip()
        self.env_defaults["PATH"] = f"{binary_dir}:{current}"
        return self.env_defaults["PATH"]

    def execute(
        self,
        command: str | list[str],
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: bool | None = True,
        check: bool = False,
    ) -> dict[str, Any]:
        shell = True if shell is None else shell
        if not self.is_alive():
            raise RuntimeError("The local Docker sandbox is no longer running.")

        docker_command = [self.container_runtime, "exec", "--workdir", cwd or self.cwd]
        merged_env = {**self.env_defaults, **(env or {})}
        for name, value in sorted(merged_env.items()):
            docker_command.extend(["--env", f"{name}={value}"])
        docker_command.append(self.container_name)

        if shell:
            shell_command = command if isinstance(command, str) else shlex.join(command)
            docker_command.extend(["/bin/sh", "-c", shell_command])
        elif isinstance(command, list) and all(isinstance(part, str) for part in command):
            docker_command.extend(command)
        else:
            return self._error_result(
                "shell=False requires command to be a list of strings",
                "TypeError",
            )

        effective_timeout = self.runtime_timeout if timeout is None else timeout
        try:
            result = subprocess.run(
                docker_command,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            message = f"Command timed out after {effective_timeout:g} seconds"
            return {
                "stdout": stdout,
                "stderr": stderr or message,
                "output": stdout + (stderr or message),
                "returncode": -1,
                "exception_info": message,
                "extra": {"exception_type": "TimeoutExpired", "exception": message},
            }
        except OSError as exc:
            if not self.is_alive():
                raise RuntimeError("The local Docker sandbox is no longer running.") from exc
            return self._error_result(str(exc), type(exc).__name__)

        output = result.stdout + result.stderr
        response = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output": output,
            "returncode": result.returncode,
            "exception_info": "",
        }
        if check and result.returncode != 0:
            response["exception_info"] = f"Command exited with code {result.returncode}"
        return response

    @staticmethod
    def _error_result(message: str, exception_type: str) -> dict[str, Any]:
        return {
            "stdout": "",
            "stderr": message,
            "output": message,
            "returncode": -1,
            "exception_info": f"An error occurred while executing the command: {message}",
            "extra": {"exception_type": exception_type, "exception": message},
        }

    def copy_to(self, source: str | Path, destination: str) -> None:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        parent = str(PurePath(destination).parent)
        created = self.execute(["mkdir", "-p", parent], shell=False, cwd="/")
        if created["returncode"] != 0:
            raise RuntimeError(f"Could not create {parent}: {created['stderr'].strip()}")
        result = self._docker(
            "cp",
            str(source_path),
            f"{self.container_name}:{destination}",
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not copy {source_path}: {result.stderr.strip()}")

    def tunnel_url(self, port: int) -> str:
        result = self._docker(
            "port",
            self.container_name,
            f"{port}/tcp",
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise ValueError(
                f"Port {port} was not published: {(result.stderr or result.stdout).strip()}"
            )
        address = result.stdout.splitlines()[0].strip()
        try:
            host_port = int(address.rsplit(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Could not parse Docker port mapping: {address}") from exc
        return f"http://127.0.0.1:{host_port}"

    def stop(self, timeout: float = 10) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            if self._lifetime_timer is not None:
                self._lifetime_timer.cancel()
            try:
                stopped = self._docker(
                    "stop",
                    "--time",
                    str(max(1, int(timeout))),
                    self.container_name,
                    timeout=timeout + 5,
                )
                if stopped.returncode != 0:
                    self._docker("rm", "--force", self.container_name, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._docker("rm", "--force", self.container_name, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
