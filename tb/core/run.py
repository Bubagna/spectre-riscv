#!/usr/bin/env python3
"""Build and run the core's cocotb unit tests with Verilator (owner: core).

  .venv/bin/python tb/core/run.py [alu|regfile|decoder ...]

Each unit gets its own Verilator build in build/tb/core/<unit>/. Exit 0 only if all pass."""

import os
import sys
import xml.etree.ElementTree as ET

from cocotb_tools.runner import get_runner

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RTL = os.path.join(ROOT, "rtl", "core")
BUILD = os.path.join(ROOT, "build", "tb", "core")

UNITS = {
    "alu": ("rv_alu", ["rv_pkg.sv", "rv_alu.sv"], "test_alu"),
    "regfile": ("rv_regfile", ["rv_regfile.sv"], "test_regfile"),
    "decoder": ("decoder_wrap", ["rv_pkg.sv", "rv_decoder.sv"], "test_decoder"),
}


def run(unit):
    top, srcs, module = UNITS[unit]
    sources = [os.path.join(RTL, s) for s in srcs]
    if top == "decoder_wrap":
        sources.append(os.path.join(HERE, "decoder_wrap.sv"))
    bdir = os.path.join(BUILD, unit)
    # A unit alone does not use every rv_pkg constant; the full core is linted with plain -Wall.
    runner = get_runner("verilator")
    runner.build(sources=sources, hdl_toplevel=top, build_dir=bdir, build_args=["-Wall", "-Wno-UNUSEDPARAM"],
                 timescale=("1ns", "1ps"), log_file=os.path.join(bdir, "build.log"))
    xml = runner.test(hdl_toplevel=top, test_module=module, build_dir=bdir, test_dir=HERE,
                      results_xml=os.path.join(bdir, "results.xml"),
                      log_file=os.path.join(bdir, "test.log"))
    fails = sum(1 for tc in ET.parse(xml).iter("testcase")
                if tc.find("failure") is not None or tc.find("error") is not None)
    print(f"{unit}: {'PASS' if fails == 0 else 'FAIL'}")
    return fails == 0


if __name__ == "__main__":
    units = sys.argv[1:] or list(UNITS)
    ok = all([run(u) for u in units])
    sys.exit(0 if ok else 1)
