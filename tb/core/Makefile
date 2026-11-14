# Core unit tests (cocotb + Verilator). Usually run from the top: make core-unit
ROOT ?= $(abspath ../..)
PY   ?= $(ROOT)/.venv/bin/python

.PHONY: all
all:
	PATH=$(ROOT)/.venv/bin:$$PATH $(PY) run.py
