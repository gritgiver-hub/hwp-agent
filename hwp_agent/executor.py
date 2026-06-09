"""Bounded child-process executors for stateful HWP work (inspect / edit).

Why a child process: ANY Hancom COM call can occasionally hang, so each run is
isolated and time-bounded; on timeout the parent kills ONLY the Hwp.exe that
child spawned. Editing opens the ORIGINAL read-only and writes a SEPARATE dst,
so originals are never mutated.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

_ROOT = str(pathlib.Path(__file__).resolve().parents[1])  # repo root (has hwp_agent/)


def _kill_pids(pids):
    for pid in pids:
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def _read_owned(pidfile) -> List[int]:
    try:
        return [int(x) for x in (pathlib.Path(pidfile).read_text() or "").split(",") if x]
    except Exception:
        return []


_PIDS = r'''
def hwp_pids():
    import subprocess
    out = subprocess.run(["tasklist","/FI","IMAGENAME eq Hwp.exe","/FO","CSV","/NH"],
                         capture_output=True, text=True, encoding="cp949", errors="ignore").stdout
    s=set()
    for line in out.splitlines():
        p=[x.strip('"') for x in line.split('","')]
        if len(p)>=2 and p[0].lower()=="hwp.exe":
            try: s.add(int(p[1]))
            except ValueError: pass
    return s
'''

_INSPECT_CHILD = _PIDS + r'''
import sys, json
root, src, outfile, pidfile = sys.argv[1:5]
sys.path.insert(0, root)
from pyhwpx import Hwp
from hwp_agent import inspect as hi
before = hwp_pids()
hwp = Hwp(visible=False)
open(pidfile,"w").write(",".join(map(str, sorted(hwp_pids()-before))))
try:
    hwp.SetMessageBoxMode(0x2FFF1)
    hwp.open(src, arg="forceopen:true;versionwarning:false")
    idx = hi.build_index(hwp)
    json.dump(idx, open(outfile,"w",encoding="utf-8"), ensure_ascii=False)
    print("OK", flush=True)
finally:
    try: hwp.quit()
    except Exception: pass
'''

_GENERATE_CHILD = _PIDS + r'''
import sys, json, os
root, dst, blocksfile, pidfile = sys.argv[1:5]
sys.path.insert(0, root)
from pyhwpx import Hwp
from hwp_agent import edit_ops as he
blocks = json.load(open(blocksfile, encoding="utf-8"))
before = hwp_pids()
hwp = Hwp(new=True, visible=False)
open(pidfile,"w").write(",".join(map(str, sorted(hwp_pids()-before))))
try:
    hwp.SetMessageBoxMode(0x2FFF1)
    for b in blocks:
        t = b.get("type")
        try: hwp.MoveDocEnd()
        except Exception: pass
        if t in ("text", "heading"):
            hwp.insert_text(b.get("text", ""))
            if b.get("newline", True): hwp.insert_text("\n")
        elif t == "table":
            rows, cols = int(b["rows"]), int(b["cols"])
            hwp.create_table(rows=rows, cols=cols, treat_as_char=True, header=bool(b.get("header", False)))
            data = b.get("data") or []
            for r in range(1, rows + 1):
                for c in range(1, cols + 1):
                    val = ""
                    if r - 1 < len(data) and c - 1 < len(data[r - 1]):
                        val = str(data[r - 1][c - 1])
                    if val == "": continue
                    hwp.get_into_nth_table(-1)
                    if hwp.goto_addr(he._excel_addr(r, c)):
                        he._clear_cell_and_type(hwp, val)
            try: hwp.MoveDocEnd()
            except Exception: pass
            hwp.insert_text("\n")
        elif t == "image":
            p = os.path.abspath(b["path"])
            if not os.path.exists(p):
                print("ERR image not found: " + p, flush=True); continue
            ctrl = hwp.insert_picture(p, treat_as_char=bool(b.get("treat_as_char", True)))
            if b.get("width") and b.get("height"):
                try: he._set_ctrl_props(ctrl, Width=int(b["width"]), Height=int(b["height"]))
                except Exception: pass
            hwp.insert_text("\n")
    hwp.save_as(dst, "HWP")
    print("DONE " + json.dumps({"saved": os.path.exists(dst)}), flush=True)
finally:
    try: hwp.quit()
    except Exception: pass
'''

_APPLY_CHILD = _PIDS + r'''
import sys, json
root, src, dst, opsfile, pidfile = sys.argv[1:6]
sys.path.insert(0, root)
from pyhwpx import Hwp
from hwp_agent import inspect as hi, edit_ops as he
plan = json.load(open(opsfile, encoding="utf-8"))
dry = bool(plan.get("dry_run", False))
ops = plan.get("operations", [])
before = hwp_pids()
hwp = Hwp(visible=False)
open(pidfile,"w").write(",".join(map(str, sorted(hwp_pids()-before))))
try:
    hwp.SetMessageBoxMode(0x2FFF1)
    hwp.open(src, arg="forceopen:true;versionwarning:false")
    index = hi.build_index(hwp)
    results = []
    for op in ops:
        r = he.resolve_and_apply(hwp, index, op, dry)
        results.append(r)
        print("OP " + json.dumps(r, ensure_ascii=False), flush=True)
    saved = False
    if not dry and any(r.get("applied") for r in results):
        hwp.save_as(dst, "HWP"); saved = True
    print("DONE " + json.dumps({"saved": saved}, ensure_ascii=False), flush=True)
finally:
    try: hwp.quit()
    except Exception: pass
'''


def inspect_file(src, policy=None, timeout: int = 60) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Open src read-only in a bounded child and return its structure index."""
    src = policy.resolve(src) if policy else str(pathlib.Path(src).resolve())
    outfile = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    pidfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pids").name
    proc = subprocess.Popen([sys.executable, "-u", "-c", _INSPECT_CHILD, _ROOT, src, outfile, pidfile],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="ignore")
    try:
        out, _ = proc.communicate(timeout=timeout)
        if "OK" in (out or "") and pathlib.Path(outfile).stat().st_size > 0:
            return True, json.load(open(outfile, encoding="utf-8")), "OK"
        return False, None, (out or "").strip()[:300]
    except subprocess.TimeoutExpired:
        proc.kill(); _kill_pids(_read_owned(pidfile))
        return False, None, f"timed out after {timeout}s"
    finally:
        for f in (outfile, pidfile):
            try: pathlib.Path(f).unlink(missing_ok=True)
            except Exception: pass


def apply_edits(src, plan: Dict[str, Any], dst, policy=None, timeout: int = 120
                ) -> Tuple[bool, List[Dict[str, Any]], str]:
    """Apply an op plan to src, writing result to dst (src never mutated)."""
    src = policy.resolve(src) if policy else str(pathlib.Path(src).resolve())
    dst = policy.resolve(dst) if policy else str(pathlib.Path(dst).resolve())
    if pathlib.Path(src) == pathlib.Path(dst):
        raise ValueError("dst must differ from src (never overwrite the original)")
    opsfile = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    pidfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pids").name
    json.dump(plan, open(opsfile, "w", encoding="utf-8"), ensure_ascii=False)
    proc = subprocess.Popen([sys.executable, "-u", "-c", _APPLY_CHILD, _ROOT, src, dst, opsfile, pidfile],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="ignore")
    results: List[Dict[str, Any]] = []
    try:
        out, _ = proc.communicate(timeout=timeout)
        saved = False
        for line in (out or "").splitlines():
            if line.startswith("OP "):
                try: results.append(json.loads(line[3:]))
                except Exception: pass
            elif line.startswith("DONE "):
                try: saved = json.loads(line[5:]).get("saved", False)
                except Exception: pass
        ok = saved and pathlib.Path(dst).exists() and pathlib.Path(dst).stat().st_size > 0
        if plan.get("dry_run"):
            ok = len(results) > 0
        return ok, results, ("OK" if ok else (out or "").strip()[:300])
    except subprocess.TimeoutExpired:
        proc.kill(); _kill_pids(_read_owned(pidfile))
        return False, results, f"timed out after {timeout}s (committed {len(results)} ops before hang)"
    finally:
        for f in (opsfile, pidfile):
            try: pathlib.Path(f).unlink(missing_ok=True)
            except Exception: pass


def generate_document(dst, blocks: List[Dict[str, Any]], policy=None, timeout: int = 120
                      ) -> Tuple[bool, str]:
    """Create a NEW .hwp from a list of blocks (text/heading/table/image) in a
    bounded child. Each block:
      {"type":"heading"|"text", "text": str, "newline": bool}
      {"type":"table", "rows": int, "cols": int, "data": [[...]], "header": bool}
      {"type":"image", "path": str, "width": int, "height": int, "treat_as_char": bool}
    """
    dst = policy.resolve(dst) if policy else str(pathlib.Path(dst).resolve())
    blocksfile = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    pidfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pids").name
    json.dump(blocks, open(blocksfile, "w", encoding="utf-8"), ensure_ascii=False)
    proc = subprocess.Popen([sys.executable, "-u", "-c", _GENERATE_CHILD, _ROOT, dst, blocksfile, pidfile],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="ignore")
    try:
        out, _ = proc.communicate(timeout=timeout)
        saved = False
        for line in (out or "").splitlines():
            if line.startswith("DONE "):
                try: saved = json.loads(line[5:]).get("saved", False)
                except Exception: pass
        ok = saved and pathlib.Path(dst).exists() and pathlib.Path(dst).stat().st_size > 0
        return ok, ("OK" if ok else (out or "").strip()[:300])
    except subprocess.TimeoutExpired:
        proc.kill(); _kill_pids(_read_owned(pidfile))
        return False, f"timed out after {timeout}s"
    finally:
        for f in (blocksfile, pidfile):
            try: pathlib.Path(f).unlink(missing_ok=True)
            except Exception: pass
