"""Argument aliases for workspace Beads commands."""

from __future__ import annotations


def normalize_beads_args(args, unknown):
    command = [*args, *unknown]
    if not command:
        return command

    alias = command[0]
    if alias in ("comment", "add-comment", "comment-add"):
        return ["comments", "add", *command[1:]]

    return command
