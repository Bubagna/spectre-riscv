# Top-level make. Each area owns its own fragment in mk/ (see docs/SPEC.md, section 7).
ROOT  := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILD := $(ROOT)/build
PY    := $(ROOT)/.venv/bin/python

.PHONY: help
help:
	@echo "See docs/SPEC.md and docs/VERIFICATION.md for targets."

-include $(wildcard $(ROOT)/mk/*.mk)
