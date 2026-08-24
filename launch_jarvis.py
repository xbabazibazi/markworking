import subprocess
from pathlib import Path

BASE    = Path(r"C:\Users\DUR\Desktop\Proje\Mark-LI-main")
PYTHONW = BASE / "venv" / "Scripts" / "pythonw.exe"
MAIN_PY = BASE / "main.py"

subprocess.Popen(
    [str(PYTHONW), str(MAIN_PY)],
    cwd=str(BASE),
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
)
