#!/usr/bin/env python3
"""Stand-in for build/sim/Vtb_top that runs the ISS, used to test the verification
scripts themselves (scripts/test_verif.py) without the RTL.

  scripts/mock_sim.py +elf=prog.elf +trace=out.trace [+max_cycles=N] [+stall=S] [+seed=K] [+bp=T]

MOCK_BUG=<n> in the environment corrupts the rd value of the n-th traced line that
writes a register, the way a forwarding bug would, so tests can check that the
comparator and the regression runner catch it.
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model"))
import iss as I  # noqa: E402


def main():
    opts = {}
    for a in sys.argv[1:]:
        if a.startswith("+") and "=" in a:
            k, v = a[1:].split("=", 1)
            opts[k] = v
    sim = I.ISS(out=sys.stdout)
    sim.load_elf(opts["elf"])
    buf = io.StringIO()
    res = sim.run(int(opts.get("max_cycles", 10_000_000)), buf if "trace" in opts else None)
    if "trace" in opts:
        lines = buf.getvalue().splitlines()
        bug = int(os.environ.get("MOCK_BUG", "0"))
        if bug:
            seen = 0
            for i, l in enumerate(lines):
                f = l.split()
                if len(f) >= 4 and f[2].startswith("x") and not l.endswith(" csr"):
                    seen += 1
                    if seen == bug:
                        f[3] = f"{int(f[3], 16) ^ 1:08x}"
                        lines[i] = " ".join(f)
                        break
        with open(opts["trace"], "w") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
    if sim.last_char != "\n":
        print()
    print(res.line())
    print(sim.stats_line())
    return res.exit_status()


if __name__ == "__main__":
    sys.exit(main())
