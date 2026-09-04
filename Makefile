.PHONY: setup doctor verify-sources test test-modal test-chess-modal check-part1 check-swebench \
	run-calculator-agent-local \
	run-code-agent run-swebench-agent run-chess-agent \
	doctor-local run-code-agent-local check-part1-local run-swebench-agent-local \
	check-swebench-local run-chess-agent-local run-chess-agent-local-skill \
	run-obs-experiment-no-legal-moves run-obs-experiment-legal-moves \
	run-obs-deepseek-no-legal run-obs-deepseek-legal \
	run-obs-gpt-oss-no-legal run-obs-gpt-oss-legal \
	run-chess-modal instructor-eval-base instructor-eval-gold instructor-eval-patch

TASK ?= tasks/chess-terminal-move
CODE_SKILLS ?= tasks/code-skills
PATCH ?= artifacts/fix.patch
PART1_TRAJECTORY ?= artifacts/part1-trajectory.json
PRIVATE_EVAL ?= .private/chess-terminal-move
PUBLIC_EVAL ?= $(TASK)/public_tests
# One of the vendored instances under tasks/swebench/.
INSTANCE ?= django__django-15368
SWEBENCH_PATCH ?= artifacts/$(INSTANCE).patch
SWEBENCH_TRAJECTORY ?= artifacts/$(INSTANCE)-trajectory.json
TRAJECTORY ?= artifacts/part3-trajectory.json
RESULT ?= artifacts/game-result.json
MODEL ?= deepseek/deepseek-v4-flash-0731
DEEPSEEK_MODEL ?= deepseek/deepseek-v4-flash-0731
GPT_OSS_MODEL ?= openai/gpt-oss-120b
MODEL_TAG ?= $(subst /,-,$(MODEL))
COMPACT_THRESHOLD ?= 6000
STEPS ?= 200
CHESS_TIMEOUT ?= 1800
OBS_NO_LEGAL_TRAJECTORY ?= artifacts/part3-no-legal-moves-$(MODEL_TAG).json
OBS_LEGAL_TRAJECTORY ?= artifacts/part3-legal-moves-$(MODEL_TAG).json
OBS_NO_LEGAL_RESULT ?= artifacts/part3-no-legal-moves-$(MODEL_TAG)-result.json
OBS_LEGAL_RESULT ?= artifacts/part3-legal-moves-$(MODEL_TAG)-result.json
LOCAL_MODEL ?= qwen3:4b-thinking
OLLAMA_BASE_URL ?= http://127.0.0.1:11434/v1
OLLAMA_API_KEY ?= ollama
# Keep local runs independent of Docker Desktop settings. The project config
# contains no credentials; public images are pulled anonymously from Colima.
LOCAL_DOCKER_CONFIG ?= $(CURDIR)/.docker-local
COLIMA_DOCKER_HOST ?= unix://$(HOME)/.colima/default/docker.sock
LOCAL_DOCKER_ENV = DOCKER_CONFIG="$(LOCAL_DOCKER_CONFIG)" DOCKER_HOST="$(COLIMA_DOCKER_HOST)"
LOCAL_PATCH ?= artifacts/local-fix.patch
LOCAL_PART1_TRAJECTORY ?= artifacts/local-part1-trajectory.json
LOCAL_TRAJECTORY ?= artifacts/local-part3-trajectory.json
LOCAL_RESULT ?= artifacts/local-game-result.json
CALCULATION ?= What is (17 + 5) * 3?
CALCULATOR_TRAJECTORY ?= artifacts/calculator-trajectory.json

setup:
	uv sync
	git submodule update --init
	$(MAKE) verify-sources

doctor:
	uv run assignment-doctor

doctor-local:
	$(LOCAL_DOCKER_ENV) \
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	OPENAI_MODEL="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama ASSIGNMENT_BACKEND=docker \
	uv run assignment-doctor --backend docker

# Smallest end-to-end ReAct example: Ollama only, with no Modal/Colima sandbox.
run-calculator-agent-local:
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	OPENAI_MODEL="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama \
	uv run assignment-calculator "$(CALCULATION)" --model "$(LOCAL_MODEL)" \
		--trajectory $(CALCULATOR_TRAJECTORY)

verify-sources:
	uv run python -c "from assignment.task import Task; from assignment.utils.image import verify_source; verify_source(Task.load('tasks/chess-terminal-move'))"

# Fast and free. These tests fail in the starter until the corresponding TODOs
# are implemented.
test:
	uv run pytest

# Slow and billable: launches real Modal sandboxes.
test-modal:
	uv run pytest -m modal

test-chess-modal:
	uv run pytest -m modal tests/test_chess_sandbox.py

# Apply the student's generated patch to a fresh testbed and run the public
# Part 1 regression test plus the existing chess-app suite.
check-part1:
	@test -f "$(PATCH)" || (echo "Patch not found: $(PATCH). Run make run-code-agent first."; exit 1)
	uv run python scripts/evaluate.py --task $(TASK) --evaluation $(PUBLIC_EVAL) \
		--patch $(PATCH) -v

# Apply a patch to the published SWE-bench image for INSTANCE and grade it
# against that instance's FAIL_TO_PASS and PASS_TO_PASS tests.
check-swebench:
	@test -f "$(SWEBENCH_PATCH)" || (echo "Patch not found: $(SWEBENCH_PATCH). Run make run-swebench-agent INSTANCE=$(INSTANCE) first."; exit 1)
	uv run python scripts/evaluate_swebench.py $(INSTANCE) --patch $(SWEBENCH_PATCH) -v

check-part1-local:
	@test -f "$(LOCAL_PATCH)" || (echo "Patch not found: $(LOCAL_PATCH). Run make run-code-agent-local first."; exit 1)
	$(LOCAL_DOCKER_ENV) uv run python scripts/evaluate.py --backend docker --task $(TASK) \
		--evaluation $(PUBLIC_EVAL) --patch $(LOCAL_PATCH) -v

check-swebench-local:
	@test -f "$(SWEBENCH_PATCH)" || (echo "Patch not found: $(SWEBENCH_PATCH)."; exit 1)
	$(LOCAL_DOCKER_ENV) uv run python scripts/evaluate_swebench.py --backend docker $(INSTANCE) \
		--patch $(SWEBENCH_PATCH) -v

run-code-agent:
	uv run assignment-code-agent --task $(TASK) --model $(MODEL) --step-limit $(STEPS) \
		--skills-path $(CODE_SKILLS) --trajectory $(PART1_TRAJECTORY) \
		--patch-output $(PATCH)

run-code-agent-local:
	$(LOCAL_DOCKER_ENV) \
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	OPENAI_MODEL="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama ASSIGNMENT_BACKEND=docker \
	uv run assignment-code-agent --backend docker --task $(TASK) --model "$(LOCAL_MODEL)" \
		--step-limit $(STEPS) --skills-path $(CODE_SKILLS) \
		--trajectory $(LOCAL_PART1_TRAJECTORY) --patch-output $(LOCAL_PATCH)

# Run the code agent on a vendored SWE-bench instance, in its published image.
run-swebench-agent:
	uv run assignment-swebench-agent $(INSTANCE) --model $(MODEL) \
		--patch-output $(SWEBENCH_PATCH) \
		--trajectory $(SWEBENCH_TRAJECTORY) \
		--skills-path $(CODE_SKILLS) \
		--step-limit $(STEPS) $(if $(filter-out 0,$(COMPACT_THRESHOLD)),--compact-threshold-tokens $(COMPACT_THRESHOLD),)

run-swebench-agent-local:
	$(LOCAL_DOCKER_ENV) \
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	OPENAI_MODEL="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama ASSIGNMENT_BACKEND=docker \
	uv run assignment-swebench-agent --backend docker $(INSTANCE) --model "$(LOCAL_MODEL)" \
		--patch-output $(SWEBENCH_PATCH) --trajectory $(SWEBENCH_TRAJECTORY) \
		--skills-path $(CODE_SKILLS) --step-limit $(STEPS) \
		$(if $(filter-out 0,$(COMPACT_THRESHOLD)),--compact-threshold-tokens $(COMPACT_THRESHOLD),)

run-chess-agent:
	uv run assignment-play-chess --model $(MODEL) --task $(TASK) --patch $(PATCH) \
		--step-limit $(STEPS) --sandbox-timeout $(CHESS_TIMEOUT) \
		--trajectory $(TRAJECTORY) --result $(RESULT)

run-chess-agent-local:
	$(LOCAL_DOCKER_ENV) \
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	OPENAI_MODEL="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama ASSIGNMENT_BACKEND=docker \
	uv run assignment-play-chess --backend docker --model "$(LOCAL_MODEL)" \
		--task $(TASK) --patch $(LOCAL_PATCH) --step-limit $(STEPS) \
		--sandbox-timeout $(CHESS_TIMEOUT) --trajectory $(LOCAL_TRAJECTORY) \
		--result $(LOCAL_RESULT)

run-chess-agent-local-skill:
	$(LOCAL_DOCKER_ENV) \
	OPENAI_BASE_URL="$(OLLAMA_BASE_URL)" OPENAI_API_KEY="$(OLLAMA_API_KEY)" \
	="$(LOCAL_MODEL)" OPENAI_API_STYLE=ollama ASSIGNMENT_BACKEND=docker \
	uv run assignment-play-chess --backend docker --programmatic-tools \
		--skills-path tasks/chess-skills --model "$(LOCAL_MODEL)" --task $(TASK) \
		--patch $(LOCAL_PATCH) --step-limit $(STEPS) --sandbox-timeout $(CHESS_TIMEOUT) \
		--trajectory artifacts/local-part3-python-skill-trajectory.json \
		--result artifacts/local-python-skill-result.json

run-obs-experiment-no-legal-moves:
	uv run assignment-play-chess --model $(MODEL) --task $(TASK) --patch $(PATCH) \
		--omit-legal-moves --step-limit $(STEPS) --sandbox-timeout $(CHESS_TIMEOUT) \
		--trajectory $(OBS_NO_LEGAL_TRAJECTORY) --result $(OBS_NO_LEGAL_RESULT)

run-obs-experiment-legal-moves:
	uv run assignment-play-chess --model $(MODEL) --task $(TASK) --patch $(PATCH) \
		--step-limit $(STEPS) --sandbox-timeout $(CHESS_TIMEOUT) \
		--trajectory $(OBS_LEGAL_TRAJECTORY) --result $(OBS_LEGAL_RESULT)

run-obs-deepseek-no-legal:
	$(MAKE) run-obs-experiment-no-legal-moves MODEL="$(DEEPSEEK_MODEL)" MODEL_TAG=deepseek

run-obs-deepseek-legal:
	$(MAKE) run-obs-experiment-legal-moves MODEL="$(DEEPSEEK_MODEL)" MODEL_TAG=deepseek

run-obs-gpt-oss-no-legal:
	$(MAKE) run-obs-experiment-no-legal-moves MODEL="$(GPT_OSS_MODEL)" MODEL_TAG=gpt-oss

run-obs-gpt-oss-legal:
	$(MAKE) run-obs-experiment-legal-moves MODEL="$(GPT_OSS_MODEL)" MODEL_TAG=gpt-oss

run-chess-modal:
	uv run assignment-chess-modal --task $(TASK) --sandbox-timeout $(CHESS_TIMEOUT) $(if $(wildcard $(PATCH)),--patch $(PATCH),)
