"""hwp_agent — local-Windows Hancom (HWP/한글) automation agent.

Runs ONLY on a Windows desktop session with Hancom Office installed (COM
`HWPFrame.HwpObject`, driven via pyhwpx). NOT for ECS/Linux.

Phase-0 spike (verified on real government-form .hwp files):
  - open / full-text extract / read table to DataFrame
  - text find/replace, table cell edit, image insert  (all persisted)
  - save HWP; convert HWP->PDF, HWP->Word(OOXML), Word(OOXML)->HWP
  - COM rules learned:
      * ONE long-lived Hwp(visible=False) session; switch docs within it.
        quit()+recreate -> dead COM server (-2147220995).
      * SetMessageBoxMode(0x2FFF1) before risky ops auto-answers dialogs
        (required so imports don't block on a hidden modal).
      * PDF import (open_pdf) can HANG -> must be time-bounded (watchdog/subprocess).
"""
