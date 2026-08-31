.PHONY: help venv test lint check dryrun image harness prove clean

PY := .venv/bin/python
PYTEST := .venv/bin/pytest

help:
	@echo "make image    - build a full image with progress (container on a Mac)"
	@echo "make test     - unit tests (runs anywhere, no root, no Linux)"
	@echo "make dryrun   - print the full command plan for a build and a write"
	@echo "make check    - tests plus a shellcheck pass if it is installed"
	@echo "make clean    - remove the venv and build artefacts"
	@echo
	@echo "make harness  - run the real-device harnesses in a container (~3 min)"
	@echo "make prove    - boot the image and prove first boot end to end (~35 min)"
	@echo
	@echo "Integration and boot tests need Linux and root:"
	@echo "  ROOTFS=... scripts/integration-test.sh"
	@echo "  scripts/qemu-boot-test.sh <image>"

venv: .venv/bin/pytest

.venv/bin/pytest:
	python3 -m venv .venv
	.venv/bin/pip -q install "pytest==8.3.4"
	.venv/bin/pip -q install -e .

test: venv
	$(PYTEST)

# Stdlib only and no venv needed: this is the same script the container runs,
# where nothing is installed but python3.
image:
	python3 scripts/build.py $(ARGS)

dryrun: venv
	@echo "=== build ==="
	@$(PY) -m portlin --dry-run build -o /tmp/portlin-rootfs.tar.zst 2>&1 | tail -40
	@echo
	@echo "=== write, encrypted ==="
	@$(PY) -m portlin --dry-run write --target /tmp/stick.img --image-size 32G \
		--rootfs /tmp/portlin-rootfs.tar.zst --yes 2>&1 | tail -60

# The three that exercise real devices. Each one caught a bug that unit tests
# structurally cannot: they run the shipped scripts against real block devices.
harness:
	docker run --rm --privileged --platform linux/amd64 -v "$$PWD:/src" -w /src \
	  debian:trixie bash -c 'export DEBIAN_FRONTEND=noninteractive; \
	  apt-get update -qq && apt-get install -y -qq --no-install-recommends \
	    python3 gdisk e2fsprogs cryptsetup-bin util-linux mount coreutils \
	    dmsetup cloud-guest-utils >/dev/null; \
	  python3 -u scripts/test-encrypt-hook.py && \
	  python3 -u scripts/test-expand.py && \
	  python3 -u scripts/test-expand.py --encrypt'

# The full thing: boot the image, answer every prompt, verify the disk grew.
prove:
	python3 -u scripts/prove-end-to-end.py

check: test
	@if command -v shellcheck >/dev/null; then \
		shellcheck scripts/*.sh && echo "shellcheck: clean"; \
	else \
		echo "shellcheck not installed, skipping"; \
	fi

clean:
	rm -rf .venv build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
