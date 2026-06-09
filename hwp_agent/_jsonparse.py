"""Robust JSON extraction from an LLM response (self-contained; no repo deps)."""
import json
import re


def parse_llm_json(raw: str) -> dict:
    if not raw:
        return {}
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    try:
        return json.loads(s)
    except Exception:
        return {}
