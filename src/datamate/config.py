"""Runtime configuration, all overridable through environment variables."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Config:
    backend: str = field(default_factory=lambda: os.environ.get("DATAMATE_BACKEND", "vllm"))  # vllm | null
    model_path: str = field(default_factory=lambda: os.environ.get("DATAMATE_MODEL", "/models/main"))
    max_model_len: int = field(default_factory=lambda: _i("DATAMATE_MAX_MODEL_LEN", 20480))
    gpu_mem_util: float = field(default_factory=lambda: _f("DATAMATE_GPU_MEM_UTIL", 0.90))
    seed: int = field(default_factory=lambda: _i("DATAMATE_SEED", 1234))

    n_samples: int = field(default_factory=lambda: _i("DATAMATE_N_SAMPLES", 3))
    max_repairs: int = field(default_factory=lambda: _i("DATAMATE_MAX_REPAIRS", 2))
    max_tokens: int = field(default_factory=lambda: _i("DATAMATE_MAX_TOKENS", 768))
    min_agree: float = field(default_factory=lambda: _f("DATAMATE_MIN_AGREE", 0.5))  # answer only if confidence > this (scoring is +1/-1/0)
    sql_timeout_s: float = field(default_factory=lambda: _f("DATAMATE_SQL_TIMEOUT_S", 20.0))
    chunk_size: int = field(default_factory=lambda: _i("DATAMATE_CHUNK_SIZE", 64))

    # Hybrid mode: the rule-based Zalo engine answers first, the LLM only sees what it abstained on.
    zalo: bool = field(default_factory=lambda: os.environ.get("DATAMATE_ZALO", "1") != "0")
    zalo_budget_s: float = field(default_factory=lambda: _f("DATAMATE_ZALO_BUDGET_S", 3600.0))

    # Time budget: the organizers allow 6h wall-clock including model loading.
    deadline_s: float = field(default_factory=lambda: _f("DATAMATE_DEADLINE_S", 5.25 * 3600))
    soft_fraction: float = field(default_factory=lambda: _f("DATAMATE_SOFT_FRACTION", 0.85))
    start_ts: float = field(default_factory=lambda: _f("DATAMATE_START_TS", time.time()))

    log_dir: str = field(default_factory=lambda: os.environ.get("DATAMATE_LOG_DIR", "/result/logs"))

    def elapsed(self) -> float:
        return time.time() - self.start_ts
