# 11-768 Assignment 1: Build an Agent Harness

This repository contains the starter code and instructions for Assignment 1 for 11-768, the Carnegie Mellon University course on AI Agents. You can find the problem statement, along with instructions for setup and notes on grading in [ASSIGNMENT.md](ASSIGNMENT.md)

Authors: Weiwei Sun and Saujas Vaduguru, with feedback from 11-768 course staff (instructors Daniel Fried and Graham Neubig, and TAs Aditya Soni, Andy Liu, Apurva Gandhi, Demi Wang, Jiarui Liu, and Yueqi Song).

## Local execution with Colima and Ollama

For the smallest end-to-end example, run the calculator agent. It uses Ollama
and the shared ReAct loop, but does not launch Colima, Docker, or Modal:

```bash
ollama pull qwen3:4b-thinking
make run-calculator-agent-local \
  CALCULATION="What is (17 + 5) * 3?"
```

Its two tools perform one checked arithmetic operation and submit the final
answer. The prompts, tool calls, and observations are saved to
`artifacts/calculator-trajectory.json`, making this a compact example to study
before the coding and chess agents.

The original Modal runners remain the default. To run the same agent loop with
a local Docker sandbox and Ollama model endpoint:

```bash
make setup
colima start --cpu 6 --memory 12 --disk 60
docker context use colima
ollama pull qwen3:4b-thinking
ollama serve  # omit this when the Ollama app/service is already running
```

In another terminal, validate the local services and run the coding agent:

```bash
make doctor-local
make run-code-agent-local STEPS=100
make check-part1-local
```

After a patch has been generated, run the basic or programmatic chess agent:

```bash
make run-chess-agent-local
make run-chess-agent-local-skill
```

The local backend builds the task source into a disposable container; it does
not bind-mount the host checkout or Docker socket. Override container limits
with `ASSIGNMENT_DOCKER_CPUS` and `ASSIGNMENT_DOCKER_MEMORY`. On Apple Silicon,
pass `--docker-platform linux/amd64` (or set `DOCKER_DEFAULT_PLATFORM`) only for
an image that lacks an ARM variant.

If you run `assignment-doctor --backend docker` directly and it reports a
missing `docker-credential-desktop`, your global Docker configuration came from
Docker Desktop but its credential helper is no longer installed. The doctor
never edits that global file for you.

The provided local Make targets avoid that problem by using the credential-free
`.docker-local/config.json` and connecting directly to Colima's default socket.
For a named Colima profile, override the endpoint, for example:

```bash
make doctor-local \
  COLIMA_DOCKER_HOST=unix://$HOME/.colima/my-profile/docker.sock
```
