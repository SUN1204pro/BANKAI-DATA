"""End-to-end smoke test: synthetic data -> agent with a mock LLM -> submission -> evaluation.

The mock LLM replays reference SQL from the generator (test-only "oracle"). What
this checks is the plumbing: ingestion, time resolution reaching the prompt,
SQL execution, formatting, voting, output files, resume and finalize.
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from evaluate import match  # noqa: E402

from datamate.agent import Agent  # noqa: E402
from datamate.config import Config  # noqa: E402
from datamate.ingest import load_business  # noqa: E402
from datamate.io_utils import done_ids, finalize, read_jsonl  # noqa: E402
from datamate.llm import GenRequest, MockBackend  # noqa: E402
from datamate.rules import RuleBook  # noqa: E402

import run as run_module  # noqa: E402


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "synth_data.py"), "--out", str(out)], check=True)
    return out


def _question_of(req: GenRequest) -> str:
    """Pull the question text out of the (possibly repair-extended) conversation."""
    for m in req.messages:
        if "## QUESTION\n" in m["content"]:
            return m["content"].split("## QUESTION\n")[-1].split("\n\n## ")[0].strip()  # sections may follow the question
    raise AssertionError("no question in prompt")


def test_agent_answers_synthetic_questions(synth):
    oracle = json.loads((synth / "oracle_sql.json").read_text(encoding="utf-8"))
    prompts: dict[str, str] = {}

    def fn(req: GenRequest) -> list[str]:
        q = _question_of(req)
        prompts[q] = "\n".join(m["content"] for m in req.messages)
        return [json.dumps(oracle[q], ensure_ascii=False)] * req.n

    cfg = Config(backend="mock", n_samples=2, max_repairs=1)
    rules = RuleBook.load(synth / "data" / "rulebook.md")
    agent = Agent(MockBackend(fn), cfg, rules)
    questions = read_jsonl(synth / "data" / "questions.jsonl")
    gold = {g["id"]: g for g in read_jsonl(synth / "gold.jsonl")}

    cache = {}

    def get_biz(bid):
        if bid not in cache:
            cache[bid] = load_business(synth / "data" / "businesses" / bid, bid)
        return cache[bid]

    results = agent.solve_batch(questions, get_biz)
    assert len(results) == len(questions) == 8
    wrong = [(line, gold[line["id"]]) for line, _ in results if not match(line, gold[line["id"]])]
    assert wrong == []

    # The deterministic context actually reached the model.
    p = prompts["sp bán dc nhiều nhất tuần rồi là gì"]
    assert "2026-02-23 to 2026-03-01" in p            # resolved "tuần rồi" (Mon-Sun)
    assert "sp = sản phẩm" in p and "dc = được" in p  # slang hints
    assert "'hoàn thành'" in p                        # rulebook text
    assert "ten_hang" in p and 'orig header "Tên hàng"' in p
    assert "2026-02-01 to 2026-02-28" in prompts["thang truoc co bao nhieu don bi huy"]


def test_repair_loop_recovers_from_bad_sql(synth):
    oracle = json.loads((synth / "oracle_sql.json").read_text(encoding="utf-8"))
    seen_repair = []

    def fn(req: GenRequest) -> list[str]:
        q = _question_of(req)
        if req.messages[-1]["content"].startswith("That query failed"):
            seen_repair.append(req.messages[-1]["content"])
            return [json.dumps(oracle[q], ensure_ascii=False)]
        bad = {"answerable": True, "answer_type": "number", "sql": "SELECT SUM(gia_tri) FROM ban_hang"}
        return [json.dumps(bad)] * req.n

    cfg = Config(backend="mock", n_samples=1, max_repairs=2)
    agent = Agent(MockBackend(fn), cfg, RuleBook.load(synth / "data" / "rulebook.md"))
    q = [q for q in read_jsonl(synth / "data" / "questions.jsonl") if q["question"] == "tháng này doanh thu bao nhiêu"][:1]
    gold = {g["id"]: g for g in read_jsonl(synth / "gold.jsonl")}
    biz = load_business(synth / "data" / "businesses" / q[0]["business_id"], q[0]["business_id"])
    (line, entry), = agent.solve_batch(q, lambda _b: biz)
    assert seen_repair and "no such column" in seen_repair[0]
    assert match(line, gold[line["id"]])
    assert entry["candidates"][0]["repaired"] is True


def test_cli_with_null_backend_resume_and_finalize(synth, tmp_path, monkeypatch):
    monkeypatch.setenv("DATAMATE_BACKEND", "null")
    monkeypatch.setenv("DATAMATE_ZALO", "0")  # this test is about the LLM-only path
    monkeypatch.setenv("DATAMATE_LOG_DIR", str(tmp_path / "logs"))
    out = tmp_path / "result" / "submission.jsonl"
    out.parent.mkdir()
    # simulate a previous run that already answered one question and died mid-write
    first = read_jsonl(synth / "data" / "questions.jsonl")[0]["id"]
    out.write_text(json.dumps({"id": first, "type": "string", "answer": "kept"}) + '\n{"id": "q_torn', encoding="utf-8")

    argv = ["--data", str(synth / "data"), "--out", str(out)]
    assert run_module.main(argv) == 0
    rows = read_jsonl(out)
    ids = [r["id"] for r in rows]
    assert ids.count(first) == 1 and rows[0]["answer"] == "kept"   # not redone
    assert all(r == {"id": r["id"], "type": "abstain"} for r in rows[1:])   # null backend abstains, rulebook shape
    assert (tmp_path / "logs" / "qa_shard0.jsonl").exists()

    assert len(ids) == 8 and len(set(ids)) == 8                    # torn line did not swallow an answer
    summary = finalize(synth / "data" / "questions.jsonl", out)
    assert summary == {"questions": 8, "filled_with_abstain": 0}
    final = read_jsonl(out)
    assert [r["id"] for r in final] == [q["id"] for q in read_jsonl(synth / "data" / "questions.jsonl")]
    assert done_ids(out) == set(r["id"] for r in final)


def test_sharding_covers_every_question_once(synth, tmp_path, monkeypatch):
    monkeypatch.setenv("DATAMATE_BACKEND", "null")
    monkeypatch.setenv("DATAMATE_LOG_DIR", str(tmp_path / "logs"))
    out = tmp_path / "submission.jsonl"
    for shard in range(3):
        run_module.main(["--data", str(synth / "data"), "--out", str(out), "--shard", str(shard), "--num-shards", "3"])
    ids = [r["id"] for r in read_jsonl(out)]
    assert sorted(ids) == sorted(q["id"] for q in read_jsonl(synth / "data" / "questions.jsonl"))


def test_hybrid_zalo_first_then_llm_only_for_its_abstentions(synth, tmp_path, monkeypatch):
    monkeypatch.setenv("DATAMATE_LOG_DIR", str(tmp_path / "logs"))
    seen: list[str] = []

    def fn(req: GenRequest) -> list[str]:
        seen.append(_question_of(req))
        return [json.dumps({"answerable": True, "answer_type": "number", "sql": "SELECT 42"})] * req.n

    monkeypatch.setattr(run_module, "make_backend", lambda cfg: MockBackend(fn))
    out = tmp_path / "submission.jsonl"
    questions = read_jsonl(synth / "data" / "questions.jsonl")
    assert run_module.main(["--data", str(synth / "data"), "--out", str(out)]) == 0

    rows = {r["id"]: r for r in read_jsonl(out)}
    assert set(rows) == {q["id"] for q in questions}
    qa = {r["id"]: r for r in read_jsonl(tmp_path / "logs" / "qa_shard0.jsonl") if r.get("source") == "zalo"}
    kept = {i for i, r in qa.items() if not r.get("to_llm")}
    handed = {i for i, r in qa.items() if r.get("to_llm")}
    assert kept and handed and kept.isdisjoint(handed)
    texts = {q["id"]: q["question"] for q in questions}
    assert set(seen) == {texts[i] for i in handed}                      # the model never saw a question Zalo kept
    assert all(rows[i] == qa[i]["final"] for i in kept)                  # Zalo's answer is written unchanged
    assert all(rows[i]["answer"] == 42 for i in handed)                  # the rest came from the LLM


def test_hybrid_skips_model_when_zalo_answers_everything(synth, tmp_path, monkeypatch):
    monkeypatch.setenv("DATAMATE_LOG_DIR", str(tmp_path / "logs"))
    from datamate import zalo_stage

    def fake_run_zalo(todo, *a, **k):
        return {q["id"]: zalo_stage.ZaloResult({"id": q["id"], "type": "number", "answer": 1}, "accepted") for q in todo}

    def boom(cfg):
        raise AssertionError("model must not load")

    monkeypatch.setattr(run_module, "run_zalo", fake_run_zalo)
    monkeypatch.setattr(run_module, "make_backend", boom)
    out = tmp_path / "submission.jsonl"
    assert run_module.main(["--data", str(synth / "data"), "--out", str(out)]) == 0
    assert all(r["answer"] == 1 for r in read_jsonl(out))
