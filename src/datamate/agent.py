"""The question-answering agent: batched generate -> execute -> repair -> vote -> answer / unanswerable / abstain.

Scoring is +1 correct, -1 wrong, 0 abstain, so the agent answers only when it is more likely right
than wrong. Confidence = share of ALL sampled candidates that agree on the winning outcome
(failed candidates count against it). Answer when confidence > cfg.min_agree (default 0.5);
calibrate that threshold on the dev set.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .context import build_context
from .execute import SqlError, run_sql
from .fmt import canon_key, format_answer, make_line
from .ingest import Business
from .llm import LLM, GenRequest
from .lunar import LunarCalendar
from .parsing import parse_date
from .prompts import build_system, extract_response, make_messages, render_user, repair_messages
from .rules import RuleBook
from .timeparse import Conventions

log = logging.getLogger("datamate.agent")


@dataclass
class Cand:
    status: str                    # ok | unanswerable | error
    answer_type: str = ""
    answer: Any = None
    sql: str = ""
    error: str = ""
    raw: str = ""
    reason: str = ""
    repaired: bool = False


@dataclass
class Item:
    q: dict
    biz: Business | None
    messages: list[dict] = field(default_factory=list)
    cands: list[Cand] = field(default_factory=list)
    info: dict = field(default_factory=dict)


class Agent:
    def __init__(
        self,
        llm: LLM,
        cfg: Config,
        rules: RuleBook,
        cal: LunarCalendar | None = None,
        conv: Conventions | None = None,
    ):
        self.llm, self.cfg, self.rules, self.cal = llm, cfg, rules, cal
        self.conv = conv or Conventions()
        self.system = build_system(rules.text)

    # ----------------------------------------------------------- preparation
    def _prepare(self, q: dict, get_biz: Callable[[str], Business | None]) -> Item:
        biz = get_biz(q["business_id"])
        if biz is None or not biz.tables:
            return Item(q, None, info={"problem": "business data not found or empty"})
        as_of = parse_date(q.get("as_of_date"))
        if as_of is None:
            return Item(q, biz, info={"problem": f"bad as_of_date {q.get('as_of_date')!r}"})
        ctx = build_context(q["question"], as_of, biz, self.cal, self.conv, self.rules)
        user = render_user(q["question"], as_of.isoformat(), ctx.sections)
        it = Item(q, biz, make_messages(self.system, user))
        it.info = {"spans": [s.as_dict() for s in ctx.spans], "hints": ctx.hints}
        return it

    # ------------------------------------------------------------ evaluation
    def _evaluate(self, it: Item, text: str) -> Cand:
        resp = extract_response(text)
        if resp is None:
            return Cand("error", error="reply was not a JSON object with a 'sql' field", raw=text)
        if resp.get("answerable") is False:
            return Cand("unanswerable", raw=text, reason=str(resp.get("reason", "")))
        sql = str(resp.get("sql") or "").strip()
        if not sql:
            return Cand("error", error="the 'sql' field is empty", raw=text)
        try:
            res = run_sql(it.biz.con, sql, self.cfg.sql_timeout_s)  # type: ignore[union-attr]
        except SqlError as e:
            return Cand("error", sql=sql, error=str(e), raw=text)
        try:
            t, a = format_answer(resp.get("answer_type"), res.cols, res.rows)
        except ValueError as e:
            return Cand(
                "error", sql=sql, raw=text,
                error=f"{e}. Check the date range, status filters and the exact entity values; "
                      "if the data truly cannot answer, reply with answerable=false.",
            )
        return Cand("ok", answer_type=t, answer=a, sql=sql, raw=text)

    # -------------------------------------------------------------- decision
    def _decide(self, it: Item) -> tuple[str, Any, str | None, dict]:
        """-> (type, answer, reason, info)."""
        total = len(it.cands)
        oks = [c for c in it.cands if c.status == "ok"]
        uns = [c for c in it.cands if c.status == "unanswerable"]
        keys = [canon_key(c.answer_type, c.answer) for c in oks]
        counts = Counter(keys)
        info: dict = {"votes": dict(counts), "unanswerable_votes": len(uns), "samples": total}

        best_n = max(counts.values()) if counts else 0
        if best_n == 0 and not uns:
            return "abstain", None, None, {**info, "why": "no executable candidate"}

        if len(uns) > best_n:  # more samples say "the data cannot answer" than agree on any value
            conf = len(uns) / total
            info["confidence"] = conf
            if conf > self.cfg.min_agree:
                reason = next((c.reason for c in uns if c.reason), "")
                return "unanswerable", None, reason, info
            return "abstain", None, None, {**info, "why": "low agreement on unanswerable"}

        best = max(counts, key=lambda k: (counts[k], -keys.index(k)))  # ties -> earliest (greedy) sample
        conf = counts[best] / total
        info["confidence"] = conf
        if conf <= self.cfg.min_agree:
            return "abstain", None, None, {**info, "why": "low agreement"}
        c = oks[keys.index(best)]
        info["sql"] = c.sql
        return c.answer_type, c.answer, None, info

    def _finish(self, it: Item) -> tuple[dict, dict]:
        if it.biz is None or not it.messages:
            t, a, reason, info = "abstain", None, None, {"why": it.info.get("problem", "not prepared")}
        else:
            t, a, reason, info = self._decide(it)
        line = make_line(it.q["id"], t, a, reason)
        entry = {
            "id": it.q["id"],
            "business_id": it.q.get("business_id"),
            "question": it.q.get("question"),
            "as_of_date": it.q.get("as_of_date"),
            "final": line,
            "decision": info,
            "context": it.info,
            "candidates": [
                {"status": c.status, "type": c.answer_type, "sql": c.sql, "error": c.error, "repaired": c.repaired}
                for c in it.cands
            ],
        }
        return line, entry

    # ------------------------------------------------------------------ main
    def solve_batch(self, questions: list[dict], get_biz: Callable[[str], Business | None]) -> list[tuple[dict, dict]]:
        items = [self._prepare(q, get_biz) for q in questions]
        live = [it for it in items if it.messages]

        reqs: list[GenRequest] = []
        owners: list[Item] = []
        for it in live:
            reqs.append(GenRequest(it.messages, n=1, temperature=0.0, max_tokens=self.cfg.max_tokens))
            owners.append(it)
            if self.cfg.n_samples > 1:
                reqs.append(GenRequest(it.messages, n=self.cfg.n_samples - 1, temperature=0.7,
                                       seed=self.cfg.seed, max_tokens=self.cfg.max_tokens))
                owners.append(it)
        outs = self.llm.generate(reqs) if reqs else []

        pending: list[tuple[Item, int]] = []
        for it, texts in zip(owners, outs):
            for t in texts:
                cand = self._evaluate(it, t)
                it.cands.append(cand)
                if cand.status == "error":
                    pending.append((it, len(it.cands) - 1))

        for _ in range(self.cfg.max_repairs):
            if not pending:
                break
            rreqs = [
                GenRequest(repair_messages(it.messages, it.cands[i].raw or "(no reply)", it.cands[i].error),
                           n=1, temperature=0.0, max_tokens=self.cfg.max_tokens)
                for it, i in pending
            ]
            routs = self.llm.generate(rreqs)
            nxt: list[tuple[Item, int]] = []
            for (it, i), texts in zip(pending, routs):
                cand = self._evaluate(it, texts[0] if texts else "")
                cand.repaired = True
                it.cands[i] = cand
                if cand.status == "error":
                    nxt.append((it, i))
            pending = nxt

        return [self._finish(it) for it in items]
