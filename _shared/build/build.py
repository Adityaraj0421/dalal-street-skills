"""Regenerate nse-budget/size.py, nse-risk/check.py and nse-broker/ticket.py from tpl/ + _shared/india_core.py
(pasted whole, verbatim) + tpl/fo.py (budget and risk only). Run from anywhere: py _shared/build/build.py"""
import os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


core = read(ROOT, "_shared", "india_core.py").split("\n")
body = "\n".join(core[11:])          # from the first import, after the module docstring
fo = read(HERE, "tpl", "fo.py")
jobs = {"size": ("nse-budget", True), "check": ("nse-risk", True), "ticket": ("nse-broker", False)}
for name, (folder, with_fo) in jobs.items():
    t = read(HERE, "tpl", name + ".py")
    hdr = "# ---- india_core (pasted verbatim from _shared/india_core.py; do not edit here) ----\n"
    out = t.replace("#@@INDIA_CORE@@", hdr + body + "\n# ---- end india_core ----\n")
    out = out.replace("#@@FO@@\n", fo + "\n" if with_fo else "")
    with open(os.path.join(ROOT, folder, name + ".py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print("built", folder, name)
