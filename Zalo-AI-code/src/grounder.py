"""Runtime indexes over the mounted `calendar.csv` and `rulebook.md`."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from src.postprocess import normalize_comparison_string, normalize_display_string


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarDay:
    """Single row from `calendar.csv`."""

    solar_date: str  # YYYY-MM-DD
    weekday: int  # 1 (Monday) .. 7 (Sunday)
    lunar_day: int
    lunar_month: int
    lunar_year: int
    lunar_leap: bool
    holiday: str


@dataclass(frozen=True)
class DateWindow:
    """Inclusive solar date window `[start_date, end_date]`."""

    start_date: str  # YYYY-MM-DD
    end_date: str  # YYYY-MM-DD
    label: str = ""
    effective_end_date: str | None = None  # Capped at as_of_date when applicable

    @property
    def num_days(self) -> int:
        d1 = date.fromisoformat(self.start_date)
        d2 = date.fromisoformat(self.end_date)
        return (d2 - d1).days + 1

    @property
    def query_end_date(self) -> str:
        return self.effective_end_date or self.end_date


# ---------------------------------------------------------------------------
# CalendarIndex (`calendar.csv`)
# ---------------------------------------------------------------------------


class CalendarIndex:
    """In-memory index over `calendar.csv` supporting Lunar/Tết and weekday queries."""

    def __init__(self, days: list[CalendarDay]) -> None:
        self.days: list[CalendarDay] = sorted(days, key=lambda d: d.solar_date)
        self.by_solar: dict[str, CalendarDay] = {d.solar_date: d for d in self.days}
        self._by_lunar: dict[tuple[int, int, int], list[CalendarDay]] = {}
        for d in self.days:
            key = (d.lunar_year, d.lunar_month, d.lunar_day)
            self._by_lunar.setdefault(key, []).append(d)

    @classmethod
    def from_csv(cls, path: str | Path) -> CalendarIndex:
        p = Path(path)
        if not p.exists():
            return cls([])
        days: list[CalendarDay] = []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                solar = (row.get("date") or "").strip()
                if not solar:
                    continue
                leap_raw = (row.get("lunar_leap") or "").strip().lower()
                days.append(
                    CalendarDay(
                        solar_date=solar,
                        weekday=int(row["weekday"]),
                        lunar_day=int(row["lunar_day"]),
                        lunar_month=int(row["lunar_month"]),
                        lunar_year=int(row["lunar_year"]),
                        lunar_leap=leap_raw in ("true", "1", "yes"),
                        holiday=normalize_display_string(row.get("holiday") or ""),
                    )
                )
        return cls(days)

    def resolve_lunar_date(
        self,
        lunar_year: int,
        lunar_month: int,
        lunar_day: int,
        *,
        prefer_leap: bool = False,
    ) -> str | None:
        """Return solar YYYY-MM-DD for a lunar date."""
        matches = self._by_lunar.get((lunar_year, lunar_month, lunar_day), [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0].solar_date
        for m in matches:
            if m.lunar_leap == prefer_leap:
                return m.solar_date
        return matches[-1].solar_date

    def resolve_tet_window(self, lunar_year: int) -> tuple[DateWindow, DateWindow] | None:
        """Resolve `Tết năm Y` and `So với trước Tết` per Rulebook §2.

        Rulebook §2:
        - `Tết năm Y, dịp Tết, mùa Tết`: từ ngày dương của 23 tháng Chạp (năm âm Y - 1)
          đến hết ngày dương của mùng 10 tháng Giêng (năm âm Y).
        - `So với trước Tết`: cùng số ngày ngay trước 23 tháng Chạp.
        """
        # 23 tháng Chạp năm âm (Y - 1). If month 12 had a leap month, take the latest 23/12.
        dec23_matches = self._by_lunar.get((lunar_year - 1, 12, 23), [])
        jan10_matches = self._by_lunar.get((lunar_year, 1, 10), [])
        if not dec23_matches or not jan10_matches:
            return None
        start_solar = dec23_matches[-1].solar_date
        end_solar = jan10_matches[0].solar_date
        tet_win = DateWindow(
            start_date=start_solar,
            end_date=end_solar,
            label=f"Tết năm {lunar_year}",
        )
        num_days = tet_win.num_days
        start_dt = date.fromisoformat(start_solar)
        pre_start_dt = start_dt - timedelta(days=num_days)
        pre_end_dt = start_dt - timedelta(days=1)
        pre_tet_win = DateWindow(
            start_date=pre_start_dt.isoformat(),
            end_date=pre_end_dt.isoformat(),
            label=f"Trước Tết năm {lunar_year}",
        )
        return tet_win, pre_tet_win

    def count_weekday_in_range(self, start_date: str, end_date: str, weekday_iso: int) -> int:
        """Count occurrences of `weekday_iso` (1=Monday .. 7=Sunday) in `[start_date, end_date]` inclusive."""
        d1 = date.fromisoformat(start_date)
        d2 = date.fromisoformat(end_date)
        if d2 < d1:
            return 0
        count = 0
        cur = d1
        while cur <= d2:
            if cur.isoweekday() == weekday_iso:
                count += 1
            cur += timedelta(days=1)
        return count


# ---------------------------------------------------------------------------
# RulebookIndex (`rulebook.md`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleClause:
    """Single rule row parsed from `rulebook.md`."""

    section_title: str
    subsection_title: str | None
    rule_name: str
    rule_body: str


class RulebookIndex:
    """Runtime parser and retriever for a split's `rulebook.md`."""

    def __init__(self, raw_text: str, clauses: list[RuleClause]) -> None:
        self.raw_text = raw_text
        self.clauses = clauses

    @classmethod
    def from_markdown(cls, path: str | Path) -> RulebookIndex:
        p = Path(path)
        if not p.exists():
            return cls("", [])
        raw_text = p.read_text(encoding="utf-8")
        clauses: list[RuleClause] = []
        current_sec = "Preamble"
        current_subsec: str | None = None
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                current_sec = line[3:].strip()
                current_subsec = None
            elif line.startswith("### "):
                current_subsec = line[4:].strip()
            elif line.startswith("|") and line.endswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 2:
                    left, right = cells[0], cells[1]
                    if (
                        not left
                        or set(left) <= {"-"}
                        or left.lower() in ("luật", "cách nói", "cách ghi", "từ", "type")
                    ):
                        continue
                    clauses.append(
                        RuleClause(
                            section_title=current_sec,
                            subsection_title=current_subsec,
                            rule_name=left,
                            rule_body=right,
                        )
                    )
        return cls(raw_text, clauses)

    def has_rule_matching(self, keyword: str) -> bool:
        kw_norm = normalize_comparison_string(keyword)
        for c in self.clauses:
            if kw_norm in normalize_comparison_string(c.rule_name) or kw_norm in normalize_comparison_string(c.rule_body):
                return True
        return False

    def select_clauses_for_question(self, question: str, industry_hint: str | None = None) -> list[RuleClause]:
        """Return general clauses + matching industry clauses for prompt/schema card grounding."""
        selected: list[RuleClause] = []
        ind_norm = normalize_comparison_string(industry_hint) if industry_hint else ""
        q_norm = normalize_comparison_string(question)
        for c in self.clauses:
            if c.subsection_title is None:
                selected.append(c)
            else:
                sub_norm = normalize_comparison_string(c.subsection_title)
                if ind_norm and (sub_norm in ind_norm or ind_norm in sub_norm):
                    selected.append(c)
                elif sub_norm in q_norm or normalize_comparison_string(c.rule_name) in q_norm:
                    selected.append(c)
        return selected
