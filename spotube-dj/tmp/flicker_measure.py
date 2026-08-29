"""How many times does the Library destroy and rebuild its rows while idle?

The state pump ticks every 4 s. Counted over a minute with nothing changing
(you are just looking at the list), before and after the fix.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spotube_dj"))
from spotube_dj import viewmodel as vm  # noqa: E402

TICKS = 15                                  # 15 x 4 s = one idle minute
ROWS = [{"id": str(i), "title": f"T{i}", "artist": "A"} for i in range(40)]
SRC = [dict(r) for r in ROWS]

rebuilds_old = TICKS                        # no content test: every tick rebuilt
rebuilds_new = 0
held_rows = held_src = None
verdicts_4s = set()
verdicts_new = set()
for k in range(TICKS):
    now = k * 4.0
    if not vm.rows_are_same(held_rows, ROWS, held_src, SRC):
        rebuilds_new += 1
        held_rows, held_src = list(ROWS), list(SRC)
    # the pump's own change detector: the bucket it used vs the shared constant
    verdicts_4s.add((40, int(now // 4)))
    verdicts_new.add((40, int(now // vm.LIBRARY_SCAN_BUCKET)))

print(f"idle minute, 40-row library, nothing changed")
print(f"  row rebuilds before : {rebuilds_old}")
print(f"  row rebuilds after  : {rebuilds_new}")
print(f"  'content changed' verdicts from the pump, 4s bucket  : {len(verdicts_4s)}")
print(f"  'content changed' verdicts from the pump, {int(vm.LIBRARY_SCAN_BUCKET)}s bucket : {len(verdicts_new)}")
