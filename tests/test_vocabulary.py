"""Unit tests for KAPPS Semantic Middleware vocabulary.

Verifies the vocabulary contract for MES + Operation-status additions
(event-trigger coordination and ontology layering).
Pure unit tests — NO GraphDB, NO network dependencies.
"""

import pytest

from kapps_semantic_middleware.vocabulary import MES, MES_NS, SVC, OperationStatus


class TestMESNamespace:
    """Test MES namespace constant."""

    def test_mes_ns_value(self):
        """MES_NS matches the published ontology namespace."""
        assert MES_NS == "https://w3id.org/circularfactory/MES#"


class TestMESTerms:
    """Test all MES vocabulary terms resolve to correct IRIs."""

    @pytest.mark.parametrize(
        "term_name",
        [
            "hasHandoverAbility",
            "complements",
            "HandoverAbility",
            "Put",
            "Receive",
            "Pick",
            "Release",
            "Pass",
            "Retrieve",
        ],
    )
    def test_mes_term_iri_suffix(self, term_name):
        """Each MES term IRI ends with MES#<term_name>."""
        term = getattr(MES, term_name)
        assert str(term).endswith("MES#" + term_name)


class TestSVCOperationStatus:
    """Test SVC.operationStatus addition."""

    def test_operation_status_iri(self):
        """SVC.operationStatus resolves to Service#operationStatus."""
        assert str(SVC.operationStatus).endswith("Service#operationStatus")


class TestOperationStatusValues:
    """Test OperationStatus string values (the Operation lifecycle)."""

    def test_queued(self):
        assert OperationStatus.QUEUED == "queued"

    def test_running(self):
        assert OperationStatus.RUNNING == "running"

    def test_done(self):
        assert OperationStatus.DONE == "done"

    def test_failed(self):
        assert OperationStatus.FAILED == "failed"

    def test_all_tuple(self):
        assert OperationStatus.ALL == ("queued", "running", "done", "failed")


class TestSVCExecutionProvenance:
    """The execution provenance terms exist under svc:."""

    def test_execution_success_removed(self):
        """svc:executionSuccess boolean is removed (success now in operationStatus)."""
        assert not hasattr(SVC, "executionSuccess")

    def test_executed_by_workflow_exists(self):
        """svc:executedByWorkflow remains for actor provenance."""
        assert hasattr(SVC, "executedByWorkflow")

    def test_execution_timestamp_exists(self):
        """svc:executionTimestamp remains for timing provenance."""
        assert hasattr(SVC, "executionTimestamp")

    def test_execution_result_exists(self):
        """svc:executionResult remains for result provenance."""
        assert hasattr(SVC, "executionResult")
