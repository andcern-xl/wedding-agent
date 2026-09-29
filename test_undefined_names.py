"""No undefined names anywhere in the bot.

29 Sep 2026 — proactive_check had crashed every morning since 14 Sep with
"NameError: name '_re' is not defined". The "Tonight" fix used `_re` inside
proactive_check, where only the other functions had `import re as _re`. The
job's except clause logged it and moved on, so the only symptom was silence —
15 days of no proactive flags for either of them.

A NameError in a scheduled job never shows up in chat testing. This catches it
statically, before push.

    pip install pyflakes && python test_undefined_names.py
"""
import glob
import subprocess
import sys

files = ["agent.py", "main.py", "self_audit.py"] + sorted(glob.glob("tools/*.py"))
out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                     capture_output=True, text=True).stdout
bad = [l for l in out.splitlines() if "undefined name" in l]
for l in bad:
    print("FAIL", l)
print("ALL PASS" if not bad else f"{len(bad)} undefined names")
raise SystemExit(1 if bad else 0)
