"""LLM backends. The agent only sees `generate(requests) -> list[list[str]]`.

* VLLMBackend - offline vLLM engine (lazy import so tests run without a GPU).
* NullBackend - returns empty text; every question becomes `abstain`.
* Tests use `MockBackend(fn)`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .config import Config


@dataclass
class GenRequest:
    messages: list[dict]
    n: int = 1
    temperature: float = 0.0
    seed: int | None = None
    max_tokens: int = 768


class LLM(Protocol):
    def generate(self, reqs: list[GenRequest]) -> list[list[str]]: ...


class NullBackend:
    def generate(self, reqs: list[GenRequest]) -> list[list[str]]:
        return [[""] * r.n for r in reqs]


class MockBackend:
    """fn(request) -> list of n strings. For tests only."""

    def __init__(self, fn: Callable[[GenRequest], list[str]]):
        self.fn = fn

    def generate(self, reqs: list[GenRequest]) -> list[list[str]]:
        return [self.fn(r) for r in reqs]


class VLLMBackend:
    """Offline vLLM engine. UNTESTED in the dev sandbox (needs a Blackwell-capable vLLM build)."""

    def __init__(self, cfg: Config):
        from vllm import LLM as VLLM  # noqa: PLC0415

        self.llm = VLLM(
            model=cfg.model_path,
            dtype="auto",
            max_model_len=cfg.max_model_len,
            gpu_memory_utilization=cfg.gpu_mem_util,
            enable_prefix_caching=True,  # the schema/rules prefix is shared across samples
            seed=cfg.seed,
        )

    def generate(self, reqs: list[GenRequest]) -> list[list[str]]:
        from vllm import SamplingParams  # noqa: PLC0415

        params = [
            SamplingParams(
                n=r.n,
                temperature=r.temperature,
                top_p=0.95 if r.temperature > 0 else 1.0,
                seed=r.seed,
                max_tokens=r.max_tokens,
            )
            for r in reqs
        ]
        outs = self.llm.chat(
            [r.messages for r in reqs],
            params,
            # Qwen3-style models: turn thinking off for latency; harmless for others.
            chat_template_kwargs={"enable_thinking": False},
            use_tqdm=False,
        )
        return [[c.text for c in o.outputs] for o in outs]


def make_backend(cfg: Config) -> LLM:
    if cfg.backend == "null":
        return NullBackend()
    return VLLMBackend(cfg)
