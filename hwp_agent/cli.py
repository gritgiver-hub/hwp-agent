"""Local CLI for the HWP agent (Windows + Hancom only).

Commands:
  doctor                         - check Hancom/COM/pyhwpx availability
  convert <src> <dst>            - HWP<->PDF/Word (direction by extension)
  inspect <src>                  - print document structure summary
  edit <src> "<지시>" [--out d] [--apply]
                                 - natural-language edit (dry-run unless --apply)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from hwp_agent.com_session import PathPolicy
from hwp_agent import convert as cv
from hwp_agent import executor as ex


def _policy_for(*paths) -> PathPolicy:
    roots = {str(pathlib.Path.cwd())}
    for p in paths:
        if p:
            roots.add(str(pathlib.Path(p).resolve().parent))
    return PathPolicy(allow_roots=sorted(roots))


def _ext(p: str) -> str:
    return pathlib.Path(p).suffix.lower().lstrip(".")


def cmd_convert(a) -> int:
    pol = _policy_for(a.src, a.dst)
    se, de = _ext(a.src), _ext(a.dst)
    pairs = {
        ("hwp", "pdf"): cv.hwp_to_pdf, ("hwpx", "pdf"): cv.hwp_to_pdf,
        ("hwp", "docx"): cv.hwp_to_docx, ("hwpx", "docx"): cv.hwp_to_docx,
        ("docx", "hwp"): cv.docx_to_hwp, ("pdf", "hwp"): cv.pdf_to_hwp,
    }
    fn = pairs.get((se, de))
    if not fn:
        print(f"지원하지 않는 변환: {se} -> {de}", file=sys.stderr)
        return 2
    ok, msg = fn(a.src, a.dst, policy=pol, timeout=a.timeout)
    print(f"{'OK' if ok else 'FAIL'}: {a.src} -> {a.dst}  ({msg})")
    return 0 if ok else 1


def cmd_inspect(a) -> int:
    from hwp_agent.inspect import summary_for_llm
    pol = _policy_for(a.src)
    ok, idx, msg = ex.inspect_file(a.src, policy=pol, timeout=a.timeout)
    if not ok:
        print(f"FAIL: {msg}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(idx, ensure_ascii=False, indent=2))
    else:
        print(summary_for_llm(idx))
    return 0


def cmd_edit(a) -> int:
    from hwp_agent import llm
    src = a.src
    dst = a.out or str(pathlib.Path(src).with_name(pathlib.Path(src).stem + "_edited" + pathlib.Path(src).suffix))
    pol = _policy_for(src, dst)
    ok, idx, msg = ex.inspect_file(src, policy=pol, timeout=a.timeout)
    if not ok:
        print(f"inspect FAIL: {msg}", file=sys.stderr)
        return 1
    plan = llm.build_plan(a.instruction, idx, dry_run=not a.apply)
    print(f"계획된 op {len(plan['operations'])}개:")
    for op in plan["operations"]:
        print("  -", json.dumps(op, ensure_ascii=False))
    ok, results, msg = ex.apply_edits(src, plan, dst, policy=pol, timeout=a.timeout)
    print(f"\n{'적용됨' if a.apply else 'DRY-RUN'} ({'OK' if ok else 'FAIL'}: {msg}):")
    for r in results:
        print(f"  [{r.get('code')}] {r.get('op_id')} conf={r.get('confidence')} "
              f"{r.get('details') or r.get('errors')}")
    if a.apply and ok:
        print(f"\n저장: {dst}")
    return 0 if ok else 1


def cmd_doctor(a) -> int:
    import subprocess
    code = ("from pyhwpx import Hwp; h=Hwp(visible=False); "
            "print('hwp_version', getattr(h,'Version', '?')); h.quit(); print('OK')")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=60)
        print(r.stdout.strip() or r.stderr.strip()[:300])
        return 0 if "OK" in (r.stdout or "") else 1
    except subprocess.TimeoutExpired:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Stop-Process -Name Hwp -Force -ErrorAction SilentlyContinue"], capture_output=True)
        print("FAIL: Hancom COM probe timed out", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hwp", description="HWP(한글) 편집 에이전트 (로컬 Windows + 한컴)")
    p.add_argument("--timeout", type=int, default=120)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("convert"); sp.add_argument("src"); sp.add_argument("dst"); sp.set_defaults(func=cmd_convert)
    sp = sub.add_parser("inspect"); sp.add_argument("src"); sp.add_argument("--json", action="store_true"); sp.set_defaults(func=cmd_inspect)
    sp = sub.add_parser("edit"); sp.add_argument("src"); sp.add_argument("instruction")
    sp.add_argument("--out"); sp.add_argument("--apply", action="store_true"); sp.set_defaults(func=cmd_edit)
    sp = sub.add_parser("doctor"); sp.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
