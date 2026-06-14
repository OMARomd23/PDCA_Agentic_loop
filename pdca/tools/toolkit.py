"""The 'standard library' injected into model-written Do scripts.

Standalone on purpose: imported by the executor's subprocess as a top-level
module, so it must not import anything from the pdca package. Workdir and
evidence-log locations arrive via environment variables set by the executor.
"""
import os
import subprocess
import sys

_WORKDIR_ENV = os.environ.get("PDCA_WORKDIR", "")
_WORKDIR = os.path.realpath(_WORKDIR_ENV) if _WORKDIR_ENV else os.path.abspath(".")
_EVIDENCE_LOG = os.environ.get("PDCA_EVIDENCE_LOG", "")
# Set by the executor from config.UNRESTRICTED. Default "1" (no jail) so the
# tools fail open to full access; the executor always sets it explicitly.
_UNRESTRICTED = os.environ.get("PDCA_UNRESTRICTED", "1") == "1"


def _resolve(path: str) -> str:
    """Map a tool path to an absolute path. Relative paths resolve against the
    working directory; absolute paths are honored as-is. In UNRESTRICTED mode
    (the default) there is no workdir jail; when disabled, escapes raise."""
    full = os.path.realpath(os.path.join(_WORKDIR, path))
    if not _UNRESTRICTED and full != _WORKDIR and not full.startswith(_WORKDIR + os.sep):
        raise ValueError(f"path escapes workdir: {path}")
    return full


def read(path: str) -> str:
    with open(_resolve(path), "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write(path: str, content: str) -> str:
    full = _resolve(path)
    os.makedirs(os.path.dirname(full) or full, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    msg = f"wrote {len(content)} chars to {path}"
    if full.endswith(".py"):
        chk = subprocess.run([sys.executable, "-m", "py_compile", full],
                             capture_output=True, text=True)
        if chk.returncode != 0:
            err = chk.stderr.strip()[-400:]
            msg += f" [SYNTAX ERROR: {err}]"
            note(f"SYNTAX ERROR in {path}: {err}")
        else:
            msg += " [syntax OK]"
    return msg


def run(cmd: str, timeout: int = 60) -> dict:
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=_WORKDIR, capture_output=True,
            text=True, timeout=timeout,
        )
        return {"code": p.returncode, "out": p.stdout, "err": p.stderr}
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "", "err": f"timeout after {timeout}s"}


def ls(path: str = ".") -> list:
    return sorted(os.listdir(_resolve(path)))


def note(text: str) -> None:
    if _EVIDENCE_LOG:
        with open(_EVIDENCE_LOG, "a", encoding="utf-8") as f:
            f.write(str(text).rstrip("\n") + "\n")


# Pasted verbatim into the Do prompt — the model's ONLY API reference.
TOOLKIT_DOCS = '''\
Available functions (already imported, do NOT import anything else for them):

  read(path) -> str
      Return a file's full text. Path is relative to the working directory.
      e.g. src = read("app/utils.py")

  write(path, content) -> str
      Overwrite (or create) a file with content; creates parent dirs.
      For .py files the return value includes a syntax-check verdict
      ("[syntax OK]" or "[SYNTAX ERROR: ...]") — ALWAYS print it and stop
      relying on a file you just wrote if its syntax check failed.
      e.g. print(write("app/utils.py", fixed_source))

  run(cmd, timeout=60) -> dict with keys "code", "out", "err"
      Run a shell command. Any command is permitted, including sudo and commands
      that touch paths outside the working directory. The command's working
      directory is the task working directory; use absolute paths to act
      elsewhere. The timeout is a hard cap (the command is killed if exceeded).
      e.g. r = run("pytest -q"); print("exit:", r["code"], r["err"][-300:])

  ls(path=".") -> list[str]
      List directory entries.
      e.g. print(ls("tests"))

  note(text) -> None
      Append a line to the cycle's evidence log (recorded findings that do
      not count against the stdout cap).
      e.g. note("bug found: off-by-one in slice at utils.py line 14")

Paths: relative paths resolve against the working directory; absolute paths
(e.g. "/tmp/out.txt", "/home/you/notes.md") are honored as given. UNRESTRICTED
MODE is active: read/write/run/ls may operate anywhere on the filesystem the OS
permits, including via sudo. There is no workdir jail — act deliberately.
'''
