"""Single long-lived Hancom COM session + path-safety policy.

Design (from Phase-0 spike + Claude/Gemini/Codex review):
- Keep ONE Hwp(visible=False) for the whole run; switch documents within it.
  Never quit()+recreate per file (that yields a dead COM server).
- Call SetMessageBoxMode(0x2FFF1) so Hancom auto-answers modal dialogs; without
  it, importing .docx/.pdf blocks on a hidden modal.
- PathPolicy enforces an allow-list and a hard deny-list (e.g. the user's Google
  Drive G: must never be written) so automation can't touch unintended files.

Note: PDF import (open_pdf) can still hang; bound it with a watchdog/subprocess
(see convert.pdf_to_hwp_bounded) rather than calling it on this in-process session.
"""
from __future__ import annotations

import pathlib
from typing import Optional

# pyhwpx uses this mask before risky ops to auto-answer message boxes.
MBOX_AUTO_ANSWER = 0x2FFF1

# Hancom Open/SaveAs format codes (pyhwpx docstring).
FORMAT = {
    "hwp": "HWP", "hwpx": "HWPX", "hwpml": "HWPML2X",
    "pdf": "PDF", "docx": "OOXML", "doc": "DOCRTF",
    "rtf": "RTF", "html": "HTML", "txt": "TEXT",
}


def _under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class PathPolicy:
    """Resolve + authorize file paths. deny takes precedence over allow."""

    def __init__(self, allow_roots=(), deny_roots=()):
        self.allow = [pathlib.Path(p).resolve() for p in allow_roots]
        self.deny = [pathlib.Path(p).resolve() for p in deny_roots]

    def resolve(self, path: str) -> str:
        rp = pathlib.Path(path).resolve()
        if any(_under(rp, d) for d in self.deny):
            raise PermissionError(f"path under deny_roots: {rp}")
        if self.allow and not any(_under(rp, a) for a in self.allow):
            raise PermissionError(f"path outside allow_roots: {rp}")
        return str(rp)


def fmt_code(name_or_ext: Optional[str], path: Optional[str] = None) -> Optional[str]:
    """Map a friendly name/extension to a Hancom format code (or None=auto)."""
    if name_or_ext:
        key = name_or_ext.lower().lstrip(".")
        return FORMAT.get(key, name_or_ext)
    if path:
        return FORMAT.get(pathlib.Path(path).suffix.lower().lstrip("."))
    return None


class HwpSession:
    """Context manager holding a single Hancom COM session."""

    def __init__(self, policy: Optional[PathPolicy] = None, visible: bool = False):
        self.policy = policy
        self.visible = visible
        self.hwp = None

    def __enter__(self) -> "HwpSession":
        from pyhwpx import Hwp  # imported lazily (Windows + Hancom only)
        self.hwp = Hwp(visible=self.visible)
        self.hwp.SetMessageBoxMode(MBOX_AUTO_ANSWER)
        return self

    def __exit__(self, *exc):
        try:
            if self.hwp is not None:
                self.hwp.quit()
        except Exception:
            pass
        self.hwp = None

    def _auth(self, path: str) -> str:
        return self.policy.resolve(path) if self.policy else str(pathlib.Path(path).resolve())

    def open(self, path: str, fmt: Optional[str] = None,
             arg: str = "forceopen:true;versionwarning:false") -> bool:
        p = self._auth(path)
        code = fmt_code(fmt, p)
        if code:
            return self.hwp.open(p, code, arg=arg)
        return self.hwp.open(p, arg=arg)

    def save_as(self, path: str, fmt: Optional[str] = None) -> bool:
        p = self._auth(path)
        code = fmt_code(fmt, p) or "HWP"
        return self.hwp.save_as(p, code)

    def get_text(self) -> str:
        return self.hwp.GetTextFile("TEXT", "") or ""

    def new_blank(self) -> None:
        self.hwp.Run("FileNew")
