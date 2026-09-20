#!/usr/bin/env python3
"""Minimal RFC 5545 sanity checks for a generated .ics file.

    python validate_ics.py public/football.ics
"""

import pathlib
import sys


def unfold(raw: str) -> list[str]:
    lines, buffer = [], ""
    for line in raw.split("\r\n"):
        if line.startswith(" "):
            buffer += line[1:]
        else:
            if buffer:
                lines.append(buffer)
            buffer = line
    if buffer:
        lines.append(buffer)
    return lines


def check(path: str) -> int:
    # Read as bytes so Python's universal-newline translation doesn't hide
    # whether the file really uses CRLF.
    raw = pathlib.Path(path).read_bytes().decode("utf-8")
    problems = []

    if "\r\n" not in raw:
        problems.append("file does not use CRLF line endings")
    if "\n" in raw.replace("\r\n", ""):
        problems.append("found a bare LF outside a CRLF pair")

    for i, physical in enumerate(raw.split("\r\n"), 1):
        if len(physical.encode("utf-8")) > 75:
            problems.append(f"line {i} exceeds 75 octets ({len(physical.encode())})")

    lines = unfold(raw)
    if lines[0] != "BEGIN:VCALENDAR":
        problems.append("missing BEGIN:VCALENDAR")
    if lines[-1] not in ("END:VCALENDAR", ""):
        problems.append(f"file ends with {lines[-1]!r}, expected END:VCALENDAR")

    depth, uids, events = 0, [], 0
    current = {}
    for line in lines:
        if line == "BEGIN:VEVENT":
            depth += 1
            events += 1
            current = {}
        elif line == "END:VEVENT":
            depth -= 1
            for required in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
                if required not in current:
                    problems.append(f"event {events} is missing {required}")
            uids.append(current.get("UID"))
            if "SEQUENCE" in current and not current["SEQUENCE"].isdigit():
                problems.append(f"event {events} has a non-numeric SEQUENCE")
        elif depth > 0 and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";")[0]] = value

    if depth != 0:
        problems.append("unbalanced BEGIN/END:VEVENT")
    duplicates = {u for u in uids if uids.count(u) > 1}
    if duplicates:
        problems.append(f"duplicate UIDs: {sorted(duplicates)[:5]}")

    print(f"{path}: {events} events, {len(set(uids))} unique UIDs")
    for problem in problems:
        print(f"  FAIL  {problem}")
    if not problems:
        print("  OK    structure, folding, line endings and UIDs all check out")
    return 1 if problems else 0


if __name__ == "__main__":
    targets = sys.argv[1:] or ["public/football.ics"]
    raise SystemExit(max(check(t) for t in targets))
