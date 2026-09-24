"""Measure prefilter_diff's real recall and false-positive rate.

Each case is a tiny git repo with a committed base file and an uncommitted
edit. BUGGY cases plant a real defect, CLEAN cases are harmless edits. Needs
`ollama serve` running. Usage: .venv/bin/python prefilter_recall_test.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import ollama_bridge as ob

BASE = '''def average(nums):
    return sum(nums) / len(nums)


def get_user(db, user_id):
    return db.query("SELECT * FROM users WHERE id = ?", (user_id,))


def read_config(path):
    with open(path) as f:
        return f.read()


def last_item(items):
    return items[len(items) - 1]
'''

# (name, replaced_text, new_text)
BUGGY = [
    ("sql-injection", '"SELECT * FROM users WHERE id = ?", (user_id,)',
     'f"SELECT * FROM users WHERE id = {user_id}"'),
    ("off-by-one", "items[len(items) - 1]", "items[len(items)]"),
    ("div-by-zero-check-removed", "sum(nums) / len(nums)", "sum(nums) / (len(nums) - 1)"),
    ("file-handle-leak", "    with open(path) as f:\n        return f.read()",
     "    f = open(path)\n    return f.read()"),
    ("hardcoded-secret", "def read_config(path):",
     'API_KEY = "sk-live-9f8a7d6c5b4a3210"\n\n\ndef read_config(path):'),
    ("wrong-operator", "sum(nums) / len(nums)", "sum(nums) * len(nums)"),
    ("shell-injection", "def read_config(path):",
     "import os\n\n\ndef run(cmd_arg):\n    os.system('ls ' + cmd_arg)\n\n\ndef read_config(path):"),
    ("swallowed-exception", "    with open(path) as f:\n        return f.read()",
     "    try:\n        with open(path) as f:\n            return f.read()\n    except Exception:\n        pass"),
]
CLEAN = [
    ("rename-param", "def average(nums):\n    return sum(nums) / len(nums)",
     "def average(values):\n    return sum(values) / len(values)"),
    ("add-docstring", "def last_item(items):",
     'def last_item(items):\n    """Return the final element."""'),
    ("negative-index", "items[len(items) - 1]", "items[-1]"),
    ("add-comment", "def get_user(db, user_id):", "# Look up one user by primary key.\ndef get_user(db, user_id):"),
]


def run_case(name: str, old: str, new: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "app.py").write_text(BASE)
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t",
                                                  "commit", "-qm", "base"]):
            subprocess.run(["git", *cmd], cwd=repo, check=True)
        assert old in BASE, name
        (repo / "app.py").write_text(BASE.replace(old, new))
        return ob.prefilter_diff(str(repo)).strip().split("\n")[0]


def main() -> int:
    caught = 0
    print("BUGGY (want FLAGGED):")
    for name, old, new in BUGGY:
        r = run_case(name, old, new)
        ok = r.startswith("FLAGGED")
        caught += ok
        print(f"  {'ok  ' if ok else 'MISS'} {name}: {r[:90]}")
    false_pos = 0
    print("CLEAN (want CLEAN):")
    for name, old, new in CLEAN:
        r = run_case(name, old, new)
        bad = r.startswith("FLAGGED")
        false_pos += bad
        print(f"  {'FP  ' if bad else 'ok  '} {name}: {r[:90]}")
    print(f"\nrecall {caught}/{len(BUGGY)}, false positives {false_pos}/{len(CLEAN)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
