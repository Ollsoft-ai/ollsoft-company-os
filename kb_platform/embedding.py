"""Who turns text into vectors, and who reorders search hits — the providers.

Configured the way dictation is (docs/dictation.md): a key file that only
the process spending it can read, and endpoints in /etc/kb/kb.env. Nothing
about a provider is compiled in, so another installation points this at its
own Azure OpenAI, OpenAI or Cohere account without touching code:

    /etc/kb/embed.key    0640 root:kbindexer   (scripts/install-search-keys.sh)
    /etc/kb/rerank.key   0640 root:kbindexer
    KB_EMBED_PROVIDER    azure-openai | openai | fake | none
    KB_EMBED_URL         https://<resource>.openai.azure.com   (openai: https://api.openai.com/v1)
    KB_EMBED_MODEL       the Azure DEPLOYMENT name, or the OpenAI model name
    KB_EMBED_DIMS        1024 — must match kb.embeddings.embedding's width
    KB_RERANK_PROVIDER   cohere | fake | none
    KB_RERANK_URL        the full rerank URL (Azure AI Foundry:
                         https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank)
    KB_RERANK_MODEL      e.g. Cohere-rerank-v4.0-pro
    KB_EMBED_MAX_DISTANCE  cosine distance past which a nearest section is noise
                         (default 0.62 — measured for text-embedding-3)

A missing key is not an error: the provider is simply absent, search stays
full-text, and the status says "unconfigured".

Every call reports what it cost in the provider's own units — tokens for an
embedding, search units for a rerank — taken from the RESPONSE, never
estimated, because that is what the spend ledger is built on. And no call
ever lets the upstream body escape into a log or an error message: the most
it keeps is a short machine-readable error code.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import common

EMBED_KEY_FILE = common.ETC_DIR / "embed.key"
RERANK_KEY_FILE = common.ETC_DIR / "rerank.key"
AZURE_API_VERSION = "2024-10-21"
TIMEOUT_S = 30
_CODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,48}$")


class ProviderError(Exception):
    """A classified failure. `kind` decides what the caller does with it:

    - auth / not_found / config — the provider as a whole is wrong: stop
      everything (the breaker), not this one text
    - rate — back off, honouring `retry_after` when the provider sent one
    - server / transport — the provider is unwell: the breaker again
    - bad_request — THIS input is the problem: bisect the batch and park
      only the culprit, never the whole queue
    """

    def __init__(self, kind: str, status: int | None = None, retry_after: float | None = None,
                 code: str | None = None):
        self.kind, self.status, self.retry_after, self.code = kind, status, retry_after, code
        bits = [kind] + ([f"HTTP {status}"] if status else []) + ([code] if code else [])
        super().__init__(" · ".join(bits))

    @property
    def global_failure(self) -> bool:
        return self.kind != "bad_request"


@dataclass
class Config:
    embed_provider: str = "none"
    embed_url: str = ""
    embed_model: str = ""
    dims: int = 1024
    rerank_provider: str = "none"
    rerank_url: str = ""
    rerank_model: str = ""
    max_distance: float = 0.62
    embed_key: str | None = field(default=None, repr=False)
    rerank_key: str | None = field(default=None, repr=False)

    @property
    def embed_ready(self) -> bool:
        return self.embed_provider == "fake" or (
            self.embed_provider in ("azure-openai", "openai") and bool(self.embed_key and self.embed_url
                                                                       and self.embed_model))

    @property
    def rerank_ready(self) -> bool:
        return self.rerank_provider == "fake" or (
            self.rerank_provider == "cohere" and bool(self.rerank_key and self.rerank_url and self.rerank_model))

    @property
    def model_id(self) -> str:
        """What a vector was made BY — part of its hash, so a model change is a
        new hash (a deliberate re-embed), never a silent mix of two spaces."""
        return f"{self.embed_provider}:{self.embed_model or '-'}:{self.dims}"


def _read_key(path: Path) -> str | None:
    """The same forgiving read as the dictation key: a bare key on its own
    line; a missing or unreadable file means "not configured", not a crash."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return raw.splitlines()[0].strip() if raw else None


def load_config(env: dict | None = None, read_keys: bool = True) -> Config:
    env = os.environ if env is None else env
    try:
        dims = int(env.get("KB_EMBED_DIMS", "1024"))
    except ValueError:
        dims = 1024
    cfg = Config(
        embed_provider=env.get("KB_EMBED_PROVIDER", "none").strip() or "none",
        embed_url=env.get("KB_EMBED_URL", "").strip().rstrip("/"),
        embed_model=env.get("KB_EMBED_MODEL", "").strip(),
        dims=dims,
        rerank_provider=env.get("KB_RERANK_PROVIDER", "none").strip() or "none",
        rerank_url=env.get("KB_RERANK_URL", "").strip(),
        rerank_model=env.get("KB_RERANK_MODEL", "").strip(),
    )
    # Past this cosine distance a "nearest" section is noise, not an answer.
    # 0.62 is measured for text-embedding-3 (scripts/search-eval.py
    # --calibrate): the farthest real answer in 41 questions sat at 0.61,
    # nonsense and half-typed words at 0.62–0.76. Another model needs its own.
    default_cut = {"fake": 0.95}.get(cfg.embed_provider, 0.62)
    try:
        cfg.max_distance = float(env.get("KB_EMBED_MAX_DISTANCE", "") or default_cut)
    except ValueError:
        cfg.max_distance = default_cut
    if read_keys:
        cfg.embed_key = _read_key(EMBED_KEY_FILE)
        cfg.rerank_key = _read_key(RERANK_KEY_FILE)
    return cfg


# ---- HTTP -------------------------------------------------------------------
def _retry_after(headers) -> float | None:
    v = headers.get("Retry-After") or headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, min(float(v), 3600.0))
    except ValueError:
        return None


def _error_code(body) -> str | None:
    """A provider's machine-readable error code, and nothing else of the body."""
    try:
        err = body.get("error") if isinstance(body, dict) else None
        code = (err.get("code") if isinstance(err, dict) else None) or (
            body.get("code") if isinstance(body, dict) else None)
    except AttributeError:
        return None
    return code if isinstance(code, str) and _CODE_RE.match(code) else None


def _classify(status: int, headers, body) -> ProviderError:
    code = _error_code(body)
    if status in (401, 403):
        return ProviderError("auth", status, code=code)
    if status == 404:
        return ProviderError("not_found", status, code=code)
    if status == 429:
        return ProviderError("rate", status, _retry_after(headers), code)
    if status >= 500:
        return ProviderError("server", status, _retry_after(headers), code)
    if status in (400, 413, 422):
        return ProviderError("bad_request", status, code=code)
    return ProviderError("server", status, code=code)


async def _post(session, url: str, headers: dict, payload: dict) -> dict:
    import aiohttp
    try:
        async with session.post(url, json=payload, headers=headers, allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:                  # noqa: BLE001 — a non-JSON answer is just a status
                body = None
            if r.status != 200:
                raise _classify(r.status, r.headers, body)
            if not isinstance(body, dict):
                raise ProviderError("server", r.status, code="non_json")
            return body
    except ProviderError:
        raise
    except asyncio.TimeoutError:
        raise ProviderError("transport", code="timeout") from None
    except aiohttp.ClientError as e:
        raise ProviderError("transport", code=type(e).__name__[:40]) from None


# ---- embedders --------------------------------------------------------------
@dataclass
class EmbedResult:
    vectors: list[list[float]]
    tokens: int


class AzureOpenAIEmbed:
    """Azure OpenAI (and the OpenAI endpoint of an Azure AI Foundry resource):
    the model is a DEPLOYMENT name, the key goes in `api-key`."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = (f"{cfg.embed_url}/openai/deployments/{cfg.embed_model}/embeddings"
                    f"?api-version={AZURE_API_VERSION}")

    def headers(self) -> dict:
        return {"api-key": self.cfg.embed_key or ""}

    async def embed(self, session, texts: list[str]) -> EmbedResult:
        body = await _post(session, self.url, self.headers(),
                           {"input": texts, "dimensions": self.cfg.dims})
        return _embed_result(body, len(texts), self.cfg.dims)


class OpenAIEmbed(AzureOpenAIEmbed):
    """api.openai.com (or anything OpenAI-compatible): model in the body,
    bearer token."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = f"{cfg.embed_url or 'https://api.openai.com/v1'}/embeddings"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.embed_key or ''}"}

    async def embed(self, session, texts: list[str]) -> EmbedResult:
        body = await _post(session, self.url, self.headers(),
                           {"input": texts, "model": self.cfg.embed_model, "dimensions": self.cfg.dims})
        return _embed_result(body, len(texts), self.cfg.dims)


def _embed_result(body: dict, n: int, dims: int) -> EmbedResult:
    data = body.get("data")
    if not isinstance(data, list) or len(data) != n:
        raise ProviderError("server", code="bad_shape")
    out: list[list[float]] = [None] * n                       # type: ignore[list-item]
    for item in data:
        i, vec = item.get("index"), item.get("embedding")
        if not isinstance(i, int) or not 0 <= i < n or not isinstance(vec, list) or len(vec) != dims:
            # a model that ignores `dimensions` would corrupt the index: refuse
            raise ProviderError("config", code="dims_mismatch")
        out[i] = vec
    usage = body.get("usage") or {}
    tokens = usage.get("total_tokens") or usage.get("prompt_tokens")
    if not isinstance(tokens, int) or tokens < 0:
        raise ProviderError("server", code="no_usage")        # the ledger needs the truth
    return EmbedResult(out, tokens)


# ---- rerankers --------------------------------------------------------------
@dataclass
class RerankResult:
    scores: list[float]          # aligned with the documents passed in
    units: int                   # search units billed


class CohereRerank:
    """Cohere v2 rerank — on Azure AI Foundry at
    https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank with
    `api-key`, or at api.cohere.com with a bearer token."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def headers(self) -> dict:
        key = self.cfg.rerank_key or ""
        if ".azure.com" in self.cfg.rerank_url:
            return {"api-key": key}
        return {"Authorization": f"Bearer {key}"}

    async def rerank(self, session, query: str, docs: list[str]) -> RerankResult:
        if not docs:
            return RerankResult([], 0)
        body = await _post(session, self.cfg.rerank_url, self.headers(),
                           {"model": self.cfg.rerank_model, "query": query,
                            "documents": docs, "top_n": len(docs)})
        results = body.get("results")
        if not isinstance(results, list):
            raise ProviderError("server", code="bad_shape")
        scores = [0.0] * len(docs)
        for r in results:
            i, s = r.get("index"), r.get("relevance_score")
            if isinstance(i, int) and 0 <= i < len(docs) and isinstance(s, (int, float)):
                scores[i] = float(s)
        billed = ((body.get("meta") or {}).get("billed_units") or {}).get("search_units")
        return RerankResult(scores, int(billed) if isinstance(billed, (int, float)) else 1)


# ---- the fake: deterministic, offline, for CI and the test suite -------------
_WORD = re.compile(r"\w+", re.UNICODE)


def _fake_vector(text: str, dims: int) -> list[float]:
    """Feature-hashed character trigrams, L2-normalised. Crude, but it has the
    one property the tests need: texts that share word stems land close
    together whatever language stemmer Postgres would have used."""
    v = [0.0] * dims
    for w in _WORD.findall(text.lower()):
        w = f" {w} "
        for i in range(len(w) - 2):
            h = int.from_bytes(hashlib.blake2b(w[i:i + 3].encode(), digest_size=4).digest(), "big")
            v[h % dims] += 1.0 if (h >> 31) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeEmbed:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls = 0

    async def embed(self, session, texts: list[str]) -> EmbedResult:
        self.calls += 1
        return EmbedResult([_fake_vector(t, self.cfg.dims) for t in texts],
                           sum(max(1, len(_WORD.findall(t))) for t in texts))


class FakeRerank:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls = 0

    async def rerank(self, session, query: str, docs: list[str]) -> RerankResult:
        self.calls += 1
        q = _fake_vector(query, self.cfg.dims)
        return RerankResult([sum(a * b for a, b in zip(q, _fake_vector(d, self.cfg.dims))) for d in docs],
                            1 if docs else 0)


def make_embedder(cfg: Config):
    if not cfg.embed_ready:
        return None
    return {"azure-openai": AzureOpenAIEmbed, "openai": OpenAIEmbed, "fake": FakeEmbed}[cfg.embed_provider](cfg)


def make_reranker(cfg: Config):
    if not cfg.rerank_ready:
        return None
    return {"cohere": CohereRerank, "fake": FakeRerank}[cfg.rerank_provider](cfg)


def vector_literal(vec: list[float]) -> str:
    """pgvector's text form, so no client-side adapter is needed."""
    return "[" + ",".join(f"{x:.6g}" for x in vec) + "]"
