"""Answer one question: parse it, run one metric, and format a strict submission record."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any

from src.grounder import CalendarIndex, RulebookIndex
from src.metrics import Abstain, ShopData, Unanswerable
from src.postprocess import format_submission_record
from src.question_parser import EntityIndex, answer_parsed, prepare
from src.rule_guard import RuleGuard


@dataclass
class Outcome:
    status: str  # accepted | unanswerable | abstain
    submission_record: dict[str, Any]
    diagnostics: list[str] = field(default_factory=list)
    parsed_text: str | None = None


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


class BusinessContext:
    """Per-business connection and entity index, kept open across questions."""

    def __init__(self, db_path: str | Path) -> None:
        self.conn = _open_readonly(Path(db_path))
        self.entities = EntityIndex(self.conn)

    def close(self) -> None:
        self.conn.close()


def answer_question(
    question_record: dict[str, Any],
    context: BusinessContext,
    *,
    calendar: CalendarIndex | None = None,
    rulebook: RulebookIndex | None = None,
    guard: RuleGuard | None = None,
) -> Outcome:
    """Answer one record. Any failure to understand or compute safely is an abstention, never a crash."""
    qid = str(question_record["id"])
    parsed_text: str | None = None
    try:
        as_of = str(question_record["as_of_date"])
        if guard is not None:
            blocked_by = guard.blocking_clause(str(question_record["question"]))
            if blocked_by:
                return Outcome(
                    "abstain",
                    format_submission_record(qid, "abstain"),
                    [f"Rulebook clause changed and needs review: {blocked_by}"],
                )
        data = ShopData(context.conn, as_of)
        data.require_healthy_global()
        parsed = prepare(str(question_record["question"]), as_of, context.entities, calendar)
        parsed_text = parsed.text
        answer = answer_parsed(parsed, data, context.entities)
        if answer.type in {"number", "percent", "list", "table"}:
            kind = "ratio_percent" if answer.type == "percent" else answer.measure_kind
            record = format_submission_record(qid, answer.type, answer.value, measure_kind=kind)
        else:
            record = format_submission_record(qid, answer.type, answer.value)
        return Outcome("accepted", record, parsed_text=parsed_text)
    except Unanswerable as exc:
        return Outcome("unanswerable", format_submission_record(qid, "unanswerable", reason=str(exc)), [str(exc)], parsed_text)
    except Abstain as exc:
        return Outcome("abstain", format_submission_record(qid, "abstain"), [str(exc)], parsed_text)
    except Exception as exc:  # noqa: BLE001 - one bad question must never lose the whole submission.
        return Outcome("abstain", format_submission_record(qid, "abstain"), [f"{type(exc).__name__}: {exc}"], parsed_text)
