"""Shared EBB client infrastructure (consolidated, behavior-preserving).

Every source client repeated the same boilerplate: a ``requests.Session`` with a
``User-Agent``, the identical tenacity retry policy on its fetch primitives, and a
"save the raw response if a raw_dir was given" step. Those are gathered here so the
per-client modules carry only their **source-specific** request logic (URLs,
params, headers, viewstate/clientState fields, column maps) — the §4
reverse-engineered specifics.

Nothing here changes request behavior: the retry policy, header application, and
raw-write semantics are byte-for-byte what each client did inline before.
"""
from __future__ import annotations

import pathlib
from typing import Any, Optional

import requests
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# The retry policy every client used on its fetch primitives, verbatim: retry on
# any requests error, up to 4 attempts, exponential backoff 1..20s, re-raise the
# last error. Spread into the decorator with ``@retry(**RETRY)``.
RETRY: dict[str, Any] = dict(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)


def write_raw(raw_dir: Optional[pathlib.Path], name: str, text: str) -> None:
    """Persist a raw response under ``raw_dir`` for lineage (no-op if raw_dir is None).

    Identical to the per-client ``_write_raw`` helpers / inline blocks it replaces:
    create the directory (parents, idempotent) and write the text as UTF-8.
    """
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / name).write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Exception hierarchy (additive). Transient network errors stay as plain
# ``requests.RequestException`` (already retried by RETRY); a source returning a
# WAF/JS challenge instead of data is an ``EbbChallengeError``.
# --------------------------------------------------------------------------- #


class EbbError(Exception):
    """Base class for ingestion errors raised by the ebb clients."""


class EbbChallengeError(EbbError):
    """A source returned a bot/WAF challenge instead of the requested data."""


# --------------------------------------------------------------------------- #
# Base client
# --------------------------------------------------------------------------- #


class BaseEBBClient:
    """Common construction for the source clients: data dir, timeout, and a
    ``requests.Session`` carrying the client's own header set.

    Subclasses pass their source-specific default ``data_dir``/``timeout`` and the
    exact header dict they used before, then add any extra state (heat content,
    cookies, caches). Request methods stay on the subclass — they encode the
    source-specific URL/params and decorate with ``@retry(**RETRY)``.
    """

    def __init__(
        self,
        data_dir: pathlib.Path | str,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.timeout = timeout
        self.session = session or requests.Session()
        if headers:
            self.session.headers.update(headers)
