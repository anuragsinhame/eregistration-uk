"""Run every test script in this folder.   python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
for script in sorted(HERE.glob("test_*.py")):
    print(f"\n### {script.name}")
    result = subprocess.run([sys.executable, str(script)], cwd=str(HERE.parent))
    if result.returncode != 0:
        failed.append(script.name)
print("\n" + ("FAILED: " + ", ".join(failed) if failed else "ALL TEST SCRIPTS PASSED"))
sys.exit(1 if failed else 0)
