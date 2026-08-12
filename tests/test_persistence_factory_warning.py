"""Offline tests for silencing transitional_sync_middleware's benign persistence-factory warning
(#89 item 6).

Every middleware start used to log:

    WARNING:transitional_sync_middleware.middleware.registries:No persistence factory found for
    data_model_name='resource' model_id=None contained_model_id=None field_id=None.
    Using default persistence factory.

``persist()`` (the base class) never had a factory pre-registered for it, so the
registry's own fallback branch fired on every call: harmless (it constructs the exact
``PersistenceFactory(ModelConnector)`` the fallback always would), but a warning nobody
had explained trains people to ignore warnings, and #86 is exactly the kind of bug that
hides behind one. ``SemanticMiddleware._suppress_default_persistence_warning``
pre-registers that identical fallback so the "not found" branch is never reached.

No GraphDB, no network: these tests exercise the persistence registry directly, and the
source-inspection tests read files rather than run them.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from transitional_sync_middleware.middleware.registries import ConnectionInfo

REPO_ROOT = Path(__file__).parent.parent


def _build_middleware():
    from kapps_semantic_middleware import Mode, SemanticMiddleware

    return SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri="http://example.org/PersistenceWarningTestResource",
        service_class="http://example.org/PersistenceWarningTestService",
        ogm=object(),  # Never dereferenced by anything these tests call.
        host="127.0.0.1",
        port=0,
        heartbeat_interval=None,
    )


class TestSuppressDefaultPersistenceWarning:
    def test_the_warning_fires_before_suppression(self, caplog):
        """Pins the baseline: an unregistered data_model_name really does warn, so the
        "after" assertion below is proof of a change, not a test that never could fail."""
        mw = _build_middleware()
        connection_info = ConnectionInfo(
            data_model_name="resource", model_id="http://example.org/x"
        )

        with caplog.at_level(
            logging.WARNING, logger="transitional_sync_middleware.middleware.registries"
        ):
            mw.persistence_registry.get_default_persistence_factory(
                connection_info, object
            )

        assert any(
            "No persistence factory found" in record.message for record in caplog.records
        )

    def test_the_warning_is_gone_after_suppression(self, caplog):
        mw = _build_middleware()
        connection_info = ConnectionInfo(
            data_model_name="resource", model_id="http://example.org/x"
        )

        mw._suppress_default_persistence_warning("resource")

        with caplog.at_level(
            logging.WARNING, logger="transitional_sync_middleware.middleware.registries"
        ):
            factory = mw.persistence_registry.get_default_persistence_factory(
                connection_info, object
            )

        assert not any(
            "No persistence factory found" in record.message for record in caplog.records
        )
        assert factory is not None

    def test_it_registers_the_same_fallback_the_base_class_would_have_built(self):
        """Behaviour is unchanged -- only the log line is. The registered factory's
        connector type must be the identical ModelConnector the base class's own
        fallback (`return PersistenceFactory(ModelConnector)`) constructs."""
        from transitional_sync_middleware.connect.connectors.model_connector import ModelConnector

        mw = _build_middleware()
        mw._suppress_default_persistence_warning("resource")

        connection_info = ConnectionInfo(data_model_name="resource")
        [(model_type, factory)] = mw.persistence_registry.persistence_factories[
            connection_info
        ]
        assert model_type is object
        assert factory.connector.func is ModelConnector

    def test_it_is_idempotent(self):
        """Safe to call more than once -- a caller need not track whether it already ran."""
        mw = _build_middleware()

        mw._suppress_default_persistence_warning("resource")
        mw._suppress_default_persistence_warning("resource")

        connection_info = ConnectionInfo(data_model_name="resource")
        assert len(mw.persistence_registry.persistence_factories[connection_info]) == 1


class TestBothCallSitesSuppressBeforePersisting:
    """"Asserted, not assumed" (ADR philosophy this repo already follows elsewhere):
    reads the actual source rather than trusting that the wiring stays in place.
    """

    def test_load_resource_datamodel_suppresses_before_persist(self):
        from kapps_semantic_middleware.middleware import SemanticMiddleware

        source = inspect.getsource(SemanticMiddleware._load_resource_datamodel)
        suppress_at = source.index("_suppress_default_persistence_warning")
        persist_at = source.index('self.persist("resource"')

        assert suppress_at < persist_at, (
            "_load_resource_datamodel must suppress the warning before persist() runs"
        )

    def test_controller_load_view_datamodels_suppresses_before_the_persist_loop(self):
        """_load_view_datamodels's own persist() call moved into _load_one_hit (#82: the
        per-hit fetch-and-persist step is shared with rebuild_view's joiner path), so the
        property this test pins now spans two functions: the suppression call must
        precede the loop that calls _load_one_hit, and that method must still be where
        persist() actually runs.
        """
        sys_path_marker = str(REPO_ROOT)
        import sys

        if sys_path_marker not in sys.path:
            sys.path.insert(0, sys_path_marker)
        from demo.transferunits.controller import Controller

        loader_source = inspect.getsource(Controller._load_view_datamodels)
        suppress_at = loader_source.index("_suppress_default_persistence_warning")
        # The actual call, not the docstring's own mention of the method name (which
        # precedes the code and would otherwise make this check pass vacuously).
        load_one_hit_call_at = loader_source.index("await self._load_one_hit(")
        assert suppress_at < load_one_hit_call_at, (
            "_load_view_datamodels must suppress the warning before it calls _load_one_hit"
        )

        assert 'self.persist("resource"' in inspect.getsource(Controller._load_one_hit), (
            "_load_one_hit must still be where the per-hit persist() call actually runs"
        )
