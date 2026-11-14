"""Tests of the verification scripts: comparator, random generator, regression runner.

Run: .venv/bin/python -m pytest scripts/test_verif.py -q
"""

import glob
import io
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "model"))
import cmp_trace  # noqa: E402
import iss as I  # noqa: E402
import regress  # noqa: E402

PY = os.path.join(ROOT, ".venv", "bin", "python")
MOCK = os.path.join(ROOT, "scripts", "mock_sim.py")


def write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return str(p)


REF = ["80000000 00100093 x01 00000001",
       "80000004 c0002573 x10 00000005 csr",
       "80000008 00110023 st 80000550 1 000000aa",
       "8000000c 00010703 x14 ffffffaa ld 80000550",
       "80000010 00000073 trap"]


def cmp(tmp_path, dut, ref=REF, **kw):
    out = io.StringIO()
    rc = cmp_trace.compare(write(tmp_path, "a", dut), write(tmp_path, "b", ref), out=out, **kw)
    return rc, out.getvalue()


def test_identical(tmp_path):
    assert cmp(tmp_path, REF)[0] == 0


def test_csr_value_ignored_but_register_not(tmp_path):
    d = list(REF)
    d[1] = "80000004 c0002573 x10 00000777 csr"
    assert cmp(tmp_path, d)[0] == 0
    d[1] = "80000004 c0002573 x11 00000005 csr"
    assert cmp(tmp_path, d)[0] == 1


def test_store_lanes_outside_mask_ignored_unless_strict(tmp_path):
    d = list(REF)
    d[2] = "80000008 00110023 st 80000550 1 aaaaaaaa"
    assert cmp(tmp_path, d)[0] == 0
    assert cmp(tmp_path, d, strict=True)[0] == 1
    d[2] = "80000008 00110023 st 80000550 1 000000ab"
    rc, out = cmp(tmp_path, d)
    assert rc == 1 and "store data" in out


@pytest.mark.parametrize("i,new,what", [
    (0, "80000000 00100093 x01 00000002", "value"),
    (0, "80000000 00100093", "destination"),
    (3, "8000000c 00010703 x14 ffffffaa ld 80000554", "load address"),
    (3, "8000000c 00010703 x14 ffffffaa", "load address"),
    (4, "80000010 00000073", "trap"),
    (2, "80000008 00110023 st 80000550 2 0000aa00", "store mask"),
])
def test_field_mismatches(tmp_path, i, new, what):
    d = list(REF)
    d[i] = new
    rc, out = cmp(tmp_path, d)
    assert rc == 1 and what in out and f"instruction {i + 1}" in out


def test_length_mismatch_and_context(tmp_path):
    rc, out = cmp(tmp_path, REF[:3], context=2)
    assert rc == 1 and "trace ends here" in out
    assert REF[1] in out and REF[0] not in out


def test_malformed_line(tmp_path):
    with pytest.raises(cmp_trace.TraceError):
        cmp(tmp_path, REF[:1] + ["80000004 zz"])


def test_iss_trace_lines_parse():
    sim = I.ISS(out=io.StringIO())
    elf = os.path.join(ROOT, "build", "sw", "riscv-tests", "rv32ui-p-sb.elf")
    if not os.path.exists(elf):
        pytest.skip("riscv-tests not built")
    sim.load_elf(elf)
    buf = io.StringIO()
    sim.run(100000, buf)
    for line in buf.getvalue().splitlines():
        cmp_trace.parse(line, "iss")


@pytest.mark.parametrize("seed", range(1000, 1060))
def test_random_programs_are_legal_and_terminate(seed, tmp_path, monkeypatch):
    """The generator's invariants hold: every program PASSes on the ISS (no trap, no
    access outside RAM, no timeout) and exercises the main features."""
    monkeypatch.setattr(regress, "BUILD", str(tmp_path))
    elf = regress.build_random(seed, 20000, 40)
    sim = I.ISS(out=io.StringIO())
    sim.load_elf(elf)
    res = sim.run(2_000_000)
    assert res.status == "PASS", (res.line(), res.reason)
    assert sim.branches > 10 and sim.jumps > 0


def test_regress_detects_injected_bug(tmp_path):
    if not glob.glob(os.path.join(ROOT, "build", "sw", "riscv-tests", "*.elf")):
        pytest.skip("riscv-tests not built")
    elf = os.path.join(ROOT, "build", "sw", "riscv-tests", "rv32ui-p-add.elf")
    base = [PY, os.path.join(ROOT, "scripts", "regress.py"), "--elf", elf, "--sim", MOCK,
            "--bp", "GSHARE,RANDOM", "--stall", "0,20", "--seeds", "2", "-j", "4",
            "--summary", str(tmp_path / "s.md")]
    ok = subprocess.run(base, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # GSHARE at stall 0 is deterministic (1 run); every other config runs 2 seeds.
    assert "7/7 runs passed" in ok.stdout
    bad = subprocess.run(base, capture_output=True, text=True, env=dict(os.environ, MOCK_BUG="40"))
    assert bad.returncode == 1
    assert "0/7 runs passed" in bad.stdout and "value" in bad.stdout
