#!/usr/bin/env python3
"""Supplementary structural contract for the typed A0 fault-hook migration.

This does not stand in for the production-helper behavioral harness.  It pins
the one approved source migration so a later restack cannot silently restore
the incompatible untyped hook invocation.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def require_once(text: str, fragment: str, *, label: str) -> int:
    count = text.count(fragment)
    assert count == 1, f"{label}: expected exactly one occurrence, got {count}"
    return text.index(fragment)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path,
                        help="materialized darlingserver source root")
    args = parser.parse_args()
    thread_c = args.source / "duct-tape" / "src" / "thread.c"
    text = thread_c.read_text(encoding="utf-8")

    hook_start = text.index('if (dtape_test_consume_fault("microthread.resume_before_suspend")) {')
    hook_end = text.index("\n\t\t}\n", hook_start) + len("\n\t\t}\n")
    hook = text[hook_start:hook_end]

    assert "thread->xnu_thread.suspend_count = 0;" in hook
    assert "dtape_hooks->thread_resume(thread->context);" not in hook
    typed = """dtape_hooks->thread_resume(
\t\t\t\tthread->context,
\t\t\t\tdtape_wake_kind_user_suspension,
\t\t\t\t0
\t\t\t);"""
    require_once(hook, typed, label="approved typed fault-hook call")
    assert hook.index("thread->xnu_thread.suspend_count = 0;") < hook.index(typed)
    assert text.count("dtape_hooks->thread_resume(thread->context);") == 0
    print("a0 typed-wake fault-hook structural contract: PASS")


if __name__ == "__main__":
    main()
