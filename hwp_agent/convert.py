"""Format conversions (HWP <-> PDF / Word), each in a bounded child process.

Phase-0 lesson: ANY Hancom COM call can occasionally hang (open_pdf, but also
re-opening a file / Clear). So every conversion runs in its own short-lived child
process with a per-op timeout; on timeout the parent kills ONLY the Hwp.exe that
child spawned (never the user's interactive Hancom), so the caller never hangs.

Success is judged by the output artifact (a fresh, non-empty file), because
pyhwpx open()/save_as() return values are unreliable.

Format codes: HWP, HWPX, PDF, OOXML(docx), DOCRTF(doc). src_fmt "PDF" -> open_pdf
(low-fidelity reconstruction).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

from hwp_agent.com_session import fmt_code

_CHILD = r'''
import sys, subprocess
from pyhwpx import Hwp

def hwp_pids():
    out = subprocess.run(["tasklist","/FI","IMAGENAME eq Hwp.exe","/FO","CSV","/NH"],
                         capture_output=True, text=True, encoding="cp949", errors="ignore").stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "hwp.exe":
            try: pids.add(int(parts[1]))
            except ValueError: pass
    return pids

src, src_fmt, dst, dst_fmt, pidfile = sys.argv[1:6]
before = hwp_pids()
hwp = Hwp(visible=False)
open(pidfile, "w").write(",".join(str(p) for p in sorted(hwp_pids() - before)))
try:
    hwp.SetMessageBoxMode(0x2FFF1)   # auto-answer modal dialogs
    if src_fmt == "PDF":
        hwp.open_pdf(src)
    elif src_fmt and src_fmt != "-":
        hwp.open(src, src_fmt, arg="forceopen:true;versionwarning:false")
    else:
        hwp.open(src, arg="forceopen:true;versionwarning:false")
    hwp.save_as(dst, dst_fmt if dst_fmt and dst_fmt != "-" else "HWP")
    print("OK")
finally:
    try: hwp.quit()
    except Exception: pass
'''


def _kill_pids(pids):
    for pid in pids:
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def convert(src, dst, src_fmt: Optional[str] = None, dst_fmt: Optional[str] = None,
            policy=None, timeout: int = 90) -> Tuple[bool, str]:
    """Convert src->dst in a bounded child. Returns (ok, message)."""
    src = policy.resolve(src) if policy else str(pathlib.Path(src).resolve())
    dst = policy.resolve(dst) if policy else str(pathlib.Path(dst).resolve())
    dstp = pathlib.Path(dst)
    if dstp.exists():
        try:
            dstp.unlink()
        except Exception:
            pass
    sf = fmt_code(src_fmt, src) if src_fmt else "-"
    df = fmt_code(dst_fmt, dst) or "HWP"
    pidfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pids").name
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, src, sf or "-", dst, df, pidfile],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    try:
        out, _ = proc.communicate(timeout=timeout)
        ok = dstp.exists() and dstp.stat().st_size > 0
        return ok, ("OK" if ok else f"no output ({(out or '').strip()[:200]})")
    except subprocess.TimeoutExpired:
        owned = []
        try:
            owned = [int(x) for x in (pathlib.Path(pidfile).read_text() or "").split(",") if x]
        except Exception:
            pass
        proc.kill()
        _kill_pids(owned)
        return False, f"timed out after {timeout}s; killed owned Hwp pids={owned}"
    finally:
        try:
            pathlib.Path(pidfile).unlink(missing_ok=True)
        except Exception:
            pass


def hwp_to_pdf(src, dst, **kw):
    return convert(src, dst, dst_fmt="pdf", **kw)


def hwp_to_docx(src, dst, **kw):
    return convert(src, dst, dst_fmt="docx", **kw)


def docx_to_hwp(src, dst, **kw):
    return convert(src, dst, src_fmt="docx", dst_fmt="hwp", **kw)


def pdf_to_hwp(src, dst, timeout: int = 60, **kw):
    """PDF -> HWP (low fidelity; open_pdf can hang, hence bounded)."""
    return convert(src, dst, src_fmt="pdf", dst_fmt="hwp", timeout=timeout, **kw)
