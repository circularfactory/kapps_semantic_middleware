"""The start's check of the dataset's reasoning ruleset against the one the deployment requires.

The required ruleset comes from the environment variable ``TRIPLESTORE_REQUIRED_RULESET``, never
from code: the middleware names no vendor term. The store's own answer comes from the interface's
``ruleset_get()``; the middleware does not read the vendor's configuration itself.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kapps_semantic_middleware.errors import DeploymentRuntimeError
from kapps_semantic_middleware.observation import _store_of

__all__ = ["REQUIRED_RULESET_ENV", "check_required_ruleset"]

logger = logging.getLogger(__name__)

REQUIRED_RULESET_ENV = "TRIPLESTORE_REQUIRED_RULESET"


def check_required_ruleset(ogm: Any) -> None:
    """Compare the ruleset of the dataset of ``ogm`` with the required one, and log what was found.

    - Variable unset: log one line that the ruleset is not checked; the store is not asked.
    - Store names another ruleset: raise ``DeploymentRuntimeError`` with one line, also logged once.
    - Store names no ruleset (``None``): log one line that it is unknown.
    - Store answers with an error: the error propagates unchanged.

    Raises:
        DeploymentRuntimeError: If the dataset runs a ruleset other than the required one.
    """
    required = (os.environ.get(REQUIRED_RULESET_ENV) or "").strip()
    if not required:
        logger.info(
            "No required ruleset set (%s); the ruleset is not checked",
            REQUIRED_RULESET_ENV,
        )
        return
    store = _store_of(ogm)
    found = store.ruleset_get() if store is not None else None
    if found is None:
        logger.info(
            "The ruleset of the dataset is unknown; the required ruleset is `%s`",
            required,
        )
        return
    if found != required:
        message = (
            f"dataset `{getattr(store, 'dataset', None)}` runs ruleset `{found}`; "
            f"this deployment requires `{required}`"
        )
        logger.error(message)
        raise DeploymentRuntimeError(message)
