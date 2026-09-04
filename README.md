# 11-768 Assignment 1: Build an Agent Harness

This repository contains the starter code and instructions for Assignment 1 for 11-768, the Carnegie Mellon University course on AI Agents. You can find the problem statement, along with instructions for setup and notes on grading in [ASSIGNMENT.md](ASSIGNMENT.md)

Authors: Weiwei Sun and Saujas Vaduguru, with feedback from 11-768 course staff (instructors Daniel Fried and Graham Neubig, and TAs Aditya Soni, Andy Liu, Apurva Gandhi, Demi Wang, Jiarui Liu, and Yueqi Song).

## Local execution with Colima and Ollama

The original Modal runners remain the default. To run the same agent loop with
a local Docker sandbox and Ollama model endpoint:

```bash
make setup
colima start --cpu 6 --memory 12 --disk 60
docker context use colima
ollama pull qwen3:8b
ollama serve  # omit this when the Ollama app/service is already running
```

In another terminal, validate the local services and run the coding agent:

```bash
make doctor-local LOCAL_MODEL=qwen3:8b
make run-code-agent-local LOCAL_MODEL=qwen3:8b STEPS=100
make check-part1-local
```

After a patch has been generated, run the basic or programmatic chess agent:

```bash
make run-chess-agent-local LOCAL_MODEL=qwen3:8b
make run-chess-agent-local-skill LOCAL_MODEL=qwen3:8b
```

The local backend builds the task source into a disposable container; it does
not bind-mount the host checkout or Docker socket. Override container limits
with `ASSIGNMENT_DOCKER_CPUS` and `ASSIGNMENT_DOCKER_MEMORY`. On Apple Silicon,
pass `--docker-platform linux/amd64` (or set `DOCKER_DEFAULT_PLATFORM`) only for
an image that lacks an ARM variant.

If `doctor-local` reports a missing `docker-credential-desktop`, your Docker
configuration came from Docker Desktop but its credential helper is no longer
installed. Install the helper, or remove/change the stale `credsStore` entry in
`~/.docker/config.json`; the doctor never edits that global file for you.
