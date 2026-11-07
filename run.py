#!/usr/bin/env python
"""Container entrypoint.

Reads questions + data from /data, writes /result/submission.jsonl incrementally.

  python run.py                              # one process, all questions
  python run.py --shard 0 --num-shards 2     # one of N data-parallel workers
  python run.py --finalize-only              # fill gaps with abstain, dedupe
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from datamate.agent import Agent  # noqa: E402
from datamate.config import Config  # noqa: E402
from datamate.fmt import make_line  # noqa: E402
from datamate.ingest import Business, load_business  # noqa: E402
from datamate.io_utils import AppendWriter, done_ids, finalize, read_jsonl  # noqa: E402
from datamate.layout import BusinessIndex, find_calendar, find_questions, find_rulebook  # noqa: E402
from datamate.llm import make_backend  # noqa: E402
from datamate.lunar import CalendarFormatError, LunarCalendar  # noqa: E402
from datamate.rules import RuleBook  # noqa: E402
from datamate.timeparse import Conventions  # noqa: E402
from datamate.zalo_stage import run_zalo  # noqa: E402

log = logging.getLogger("datamate")


class BusinessCache:
    def __init__(self, index: BusinessIndex, max_size: int = 24):
        self.index, self.max_size = index, max_size
        self.cache: "OrderedDict[str, Business | None]" = OrderedDict()

    def get(self, business_id: str) -> Business | None:
        if business_id in self.cache:
            self.cache.move_to_end(business_id)
            return self.cache[business_id]
        biz = None
        folder = self.index.find(business_id)
        if folder is None:
            log.warning("no data folder for %s", business_id)
        else:
            try:
                biz = load_business(folder, business_id)
            except Exception:  # noqa: BLE001
                log.error("failed to load %s:\n%s", business_id, traceback.format_exc())
        self.cache[business_id] = biz
        while len(self.cache) > self.max_size:
            _, old = self.cache.popitem(last=False)
            if old is not None:
                old.close()
        return biz

    def close_all(self) -> None:
        for biz in self.cache.values():
            if biz is not None:
                biz.close()
        self.cache.clear()


def load_conventions(path: Path) -> Conventions:
    conv = Conventions()
    if path.exists():
        for k, v in json.loads(path.read_text(encoding="utf-8")).items():
            if hasattr(conv, k):
                setattr(conv, k, v)
    return conv


def abstain(q: dict) -> dict:
    return make_line(q["id"], "abstain")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data")
    ap.add_argument("--out", default="/result/submission.jsonl")
    ap.add_argument("--questions")
    ap.add_argument("--business-root", help="folder that contains one sub-folder per business_id")
    ap.add_argument("--rulebook")
    ap.add_argument("--calendar")
    ap.add_argument("--conventions", default=str(Path(__file__).parent / "conventions.json"))
    ap.add_argument("--zalo-db-dir", help="where the Zalo engine keeps its per-business databases (default: a temp dir)")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--finalize-only", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    qa_log = None
    try:
        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(cfg.log_dir) / f"run_shard{args.shard}.log", encoding="utf-8"))
        qa_log = AppendWriter(Path(cfg.log_dir) / f"qa_shard{args.shard}.jsonl")
    except OSError as e:
        print(f"warning: cannot write logs to {cfg.log_dir}: {e}", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")

    root = Path(args.data)
    qpath = Path(args.questions) if args.questions else find_questions(root)

    if args.finalize_only:
        print(json.dumps(finalize(qpath, args.out)))
        return 0

    questions = read_jsonl(qpath)
    mine = [q for i, q in enumerate(questions) if i % args.num_shards == args.shard]
    finished = done_ids(args.out)
    todo = [q for q in mine if q["id"] not in finished]
    todo.sort(key=lambda q: q.get("business_id", ""))  # stable: keeps file order inside a business
    log.info("shard %d/%d: %d questions, %d already done, %d to do",
             args.shard, args.num_shards, len(mine), len(mine) - len(todo), len(todo))

    writer = AppendWriter(args.out)
    if not todo:
        return 0

    rulebook_path = Path(args.rulebook) if args.rulebook else find_rulebook(root)
    rules = RuleBook.load(rulebook_path)
    log.info("rulebook: %s (%d chars)", rulebook_path, len(rules.text))
    cal = None
    cal_path = Path(args.calendar) if args.calendar else find_calendar(root)
    if cal_path:
        try:
            cal = LunarCalendar.load(cal_path)
            log.info("lunar calendar: %s (%d days)", cal_path, len(cal.days))
        except (CalendarFormatError, Exception) as e:  # noqa: BLE001
            log.warning("lunar calendar disabled (%s): %s", cal_path, e)

    index = BusinessIndex(root, Path(args.business_root) if args.business_root else None)

    if cfg.zalo:
        # Stage 1: the rule-based engine. Its answers (and its provable "unanswerable") are final;
        # only the questions it abstained on, or did not reach, go to the LLM.
        shard_db = Path(args.zalo_db_dir or tempfile.mkdtemp(prefix="datamate_zalo_")) / f"shard{args.shard}"
        workers = max(1, (os.cpu_count() or 1) // max(1, args.num_shards))
        try:
            zres = run_zalo(todo, index.find, rulebook_path, cal_path, shard_db, workers, cfg.zalo_budget_s)
        except Exception:  # noqa: BLE001
            log.error("zalo stage failed, sending everything to the LLM:\n%s", traceback.format_exc())
            zres = {}
        rest: list[dict] = []
        for q in todo:
            r = zres.get(q["id"])
            if r is not None and r.final:
                writer.write(r.line)
                if qa_log:
                    qa_log.write({"id": q["id"], "source": "zalo", "status": r.status, "final": r.line,
                                  "parsed_text": r.parsed_text, "diagnostics": r.diagnostics})
            else:
                if r is not None and qa_log:
                    qa_log.write({"id": q["id"], "source": "zalo", "status": r.status, "to_llm": True,
                                  "diagnostics": r.diagnostics})
                rest.append(q)
        writer.sync()
        log.info("zalo kept %d, %d go to the LLM (elapsed %.0fs)", len(todo) - len(rest), len(rest), cfg.elapsed())
        todo = rest
        if not todo:  # nothing left for the model: do not even load it
            writer.close()
            return 0

    cache = BusinessCache(index)
    llm = make_backend(cfg)
    agent = Agent(llm, cfg, rules, cal, load_conventions(Path(args.conventions)))
    log.info("model loaded after %.0fs", cfg.elapsed())

    soft_done = False
    for i in range(0, len(todo), cfg.chunk_size):
        chunk = todo[i : i + cfg.chunk_size]
        el = cfg.elapsed()
        if el > cfg.deadline_s:
            log.error("deadline reached (%.0fs): abstaining on %d remaining questions", el, len(todo) - i)
            for q in todo[i:]:
                writer.write(abstain(q))
            break
        if el > cfg.soft_fraction * cfg.deadline_s and not soft_done:
            log.warning("soft deadline: switching to single-sample, no-repair mode")
            cfg.n_samples, cfg.max_repairs, soft_done = 1, 0, True

        try:
            results = agent.solve_batch(chunk, cache.get)
        except Exception:  # noqa: BLE001
            log.error("batch failed, retrying one by one:\n%s", traceback.format_exc())
            results = []
            for q in chunk:
                try:
                    results += agent.solve_batch([q], cache.get)
                except Exception:  # noqa: BLE001
                    log.error("question %s failed:\n%s", q.get("id"), traceback.format_exc())
                    results.append((abstain(q), {"id": q.get("id"), "error": "exception"}))
        for line, entry in results:
            writer.write(line)
            if qa_log:
                qa_log.write(entry)
        writer.sync()
        log.info("done %d/%d (elapsed %.0fs)", min(i + cfg.chunk_size, len(todo)), len(todo), cfg.elapsed())

    cache.close_all()
    writer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
