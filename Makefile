VENV := .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
DEV_REQS := requirements-dev.txt
RUNTIME_REQS := skills/inter-session/requirements.txt

# Sentinel file marks "deps are up-to-date with the reqs files". `make`
# rebuilds it whenever either reqs file is newer (or it's missing), so
# pulling new deps just means re-running `make test`.
DEPS_STAMP := $(VENV)/.deps-stamp

.PHONY: test test-fast clean help
.DEFAULT_GOAL := help

help:
	@echo "Targets (all run inside $(VENV); system Python is never touched):"
	@echo "  make test       Run the full pytest suite."
	@echo "  make test-fast  Skip subprocess-spawning tests (@pytest.mark.slow)."
	@echo "  make clean      Remove $(VENV)."

test: $(DEPS_STAMP)
	$(PYTEST) -q

test-fast: $(DEPS_STAMP)
	$(PYTEST) -q -m "not slow"

clean:
	rm -rf $(VENV)

$(DEPS_STAMP): $(DEV_REQS) $(RUNTIME_REQS)
	@if command -v uv >/dev/null 2>&1; then \
		echo "Bootstrapping $(VENV) with uv..."; \
		test -x $(PY) || uv venv $(VENV); \
		uv pip install -p $(VENV) -r $(DEV_REQS); \
	else \
		echo "Bootstrapping $(VENV) with python3 -m venv (uv not found)..."; \
		python3 -m venv $(VENV); \
		$(VENV)/bin/pip install -r $(DEV_REQS); \
	fi
	@touch $(DEPS_STAMP)
