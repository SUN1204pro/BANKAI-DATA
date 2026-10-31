"""Crash-safe output: append-only submission writing, resume, and finalize."""
from __future__ import annotations

import json
import os
from pathlib import Path


class AppendWriter:
    """One os.write per line on an O_APPEND fd, so parallel shards never interleave lines."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # After a crash the last line may be torn (no trailing newline); start on a fresh line.
        torn = False
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        if torn:
            os.write(self.fd, b"\n")

    def write(self, obj: dict) -> None:
        os.write(self.fd, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def sync(self) -> None:
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def read_jsonl(path: str | Path) -> list[dict]:
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line after a crash is simply re-done
    return out


def done_ids(path: str | Path) -> set[str]:
    return {r["id"] for r in read_jsonl(path) if "id" in r}


def finalize(questions_path: str | Path, out_path: str | Path) -> dict:
    """Make the submission complete and clean: one line per question, abstain if missing."""
    questions = read_jsonl(questions_path)
    have: dict[str, dict] = {}
    for r in read_jsonl(out_path):
        have.setdefault(r["id"], r)  # first answer wins
    lines, missing = [], 0
    for q in questions:
        r = have.get(q["id"])
        if r is None:
            r = {"id": q["id"], "type": "abstain"}
            missing += 1
        lines.append(json.dumps(r, ensure_ascii=False))
    out_path = Path(out_path)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, out_path)
    return {"questions": len(questions), "filled_with_abstain": missing}
