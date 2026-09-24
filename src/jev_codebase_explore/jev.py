"""Jev candidate ranking through Vercel AI Gateway."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .search import SearchResult


@dataclass(frozen=True)
class Candidate:
    """A bounded, locally resolved candidate that Jev may rank."""

    candidate_id: str
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    snippet: str
    lexical_score: float


@dataclass(frozen=True)
class RankingResult:
    """Provider-independent ranking result and efficiency metadata."""

    ranked: tuple[tuple[str, float], ...]
    selected_candidate: str | None
    any_candidate_probability: float
    provider: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
    estimated_cost_usd: float | None = None

    @property
    def cost_input_tokens(self) -> int:
        return self.input_tokens or 0


class DecisionRanker(Protocol):
    def rank_candidates(self, issue: str, candidates: list[Candidate]) -> RankingResult:
        ...


class HeuristicRanker:
    """Offline baseline using the local FTS score and deterministic ordering."""

    def rank_candidates(self, issue: str, candidates: list[Candidate]) -> RankingResult:
        started = time.perf_counter()
        ranked = tuple(
            (candidate.candidate_id, candidate.lexical_score)
            for candidate in sorted(
                candidates,
                key=lambda item: (-item.lexical_score, item.path, item.start_line),
            )
        )
        selected = ranked[0][0] if ranked else None
        return RankingResult(
            ranked=ranked,
            selected_candidate=selected,
            any_candidate_probability=1.0 if candidates else 0.0,
            provider="heuristic",
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class GatewayJevClient:
    """Minimal Vercel AI Gateway evaluation client."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        endpoint: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key or os.environ.get("AI_GATEWAY_API_KEY", "")
        self.model = model or os.environ.get("JEV_MODEL", "typesafe-ai/jev")
        self.endpoint = endpoint or os.environ.get(
            "JEV_EVALUATE_URL", "https://ai-gateway.vercel.sh/v1/evaluate"
        )
        self.timeout = timeout or float(os.environ.get("JEV_TIMEOUT_SECONDS", "30"))
        self.transport = transport

    def evaluate(self, state: dict[str, object], questions: dict[str, object]) -> dict[str, object]:
        if not self.api_key:
            raise RuntimeError("AI_GATEWAY_API_KEY is not configured")
        payload = {"model": self.model, "state": state, "questions": questions}
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                    response = client.post(
                        self.endpoint,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                if response.status_code in {408, 429} or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                data["_latency_ms"] = (time.perf_counter() - started) * 1000
                return data
            except (httpx.HTTPError, ValueError, KeyError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(0.05 * (2**attempt))
        raise RuntimeError(f"Jev Gateway request failed: {last_error}") from last_error


class GatewayJevRanker:
    """Ranks local candidates using one Gateway evaluation request."""

    def __init__(self, client: GatewayJevClient | None = None):
        self.client = client or GatewayJevClient()

    def rank_candidates(self, issue: str, candidates: list[Candidate]) -> RankingResult:
        if not candidates:
            return RankingResult((), None, 0.0, "vercel-ai-gateway/jev", 0.0)
        state = {
            "issue": issue,
            "candidates": [
                {
                    "id": candidate.candidate_id,
                    "path": candidate.path,
                    "symbol": candidate.name,
                    "kind": candidate.kind,
                    "lines": f"{candidate.start_line}-{candidate.end_line}",
                    "snippet": candidate.snippet[:2000],
                }
                for candidate in candidates
            ],
        }
        criteria = {
            candidate.candidate_id: f"{candidate.path}:{candidate.start_line} {candidate.name} - {candidate.snippet[:300]}"
            for candidate in candidates
        }
        data = self.client.evaluate(
            state,
            {
                "relevant_candidate": {
                    "type": "choice",
                    "instructions": "Which candidate is most likely to contain the faulty implementation for the issue?",
                    "criteria": criteria,
                },
                "any_candidate_fits": {
                    "type": "boolean",
                    "instructions": "Does any candidate contain evidence relevant to the reported issue?",
                },
            },
        )
        answers = data.get("answers", {})
        choice = answers.get("relevant_candidate", {})
        any_answer = answers.get("any_candidate_fits", {})
        probabilities = choice.get("probabilities", {})
        ranked = tuple(sorted(((key, float(value)) for key, value in probabilities.items()), key=lambda item: -item[1]))
        selected = choice.get("choice")
        any_probability = _boolean_probability(any_answer)
        usage = data.get("usage", {}) or {}
        metadata = data.get("providerMetadata", data.get("provider_metadata", {})) or {}
        gateway_metadata = metadata.get("gateway", {}) if isinstance(metadata, dict) else {}
        gateway_cost = gateway_metadata.get("marketCost") if isinstance(gateway_metadata, dict) else None
        return RankingResult(
            ranked=ranked,
            selected_candidate=selected if selected in {candidate.candidate_id for candidate in candidates} else None,
            any_candidate_probability=any_probability,
            provider="vercel-ai-gateway/jev",
            latency_ms=float(data.get("_latency_ms", 0)),
            input_tokens=_int_or_none(_first_value(usage, "input_tokens", "inputTokens")),
            output_tokens=_int_or_none(_first_value(usage, "output_tokens", "outputTokens")),
            request_id=data.get("request_id", gateway_metadata.get("generationId") if isinstance(gateway_metadata, dict) else None),
            estimated_cost_usd=_float_or_none(gateway_cost),
        )


def candidates_from_search(results: list[SearchResult]) -> list[Candidate]:
    """Convert local FTS results into stable candidate IDs."""
    return [
        Candidate(
            candidate_id=f"{result.entity_type}:{result.entity_id}",
            path=result.path,
            name=result.name,
            kind=result.kind,
            start_line=result.start_line,
            end_line=result.end_line,
            snippet=result.snippet,
            lexical_score=result.score,
        )
        for result in results
    ]


def _boolean_probability(answer: object) -> float:
    if not isinstance(answer, dict):
        return 0.0
    value = answer.get("probability", answer.get("noul", answer.get("boolean", 0.0)))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_value(mapping: object, *keys: str) -> object:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None
