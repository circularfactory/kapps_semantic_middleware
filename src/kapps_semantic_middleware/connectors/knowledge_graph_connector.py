"""
Knowledge Graph Connector for KAPPS Semantic Middleware.

This module wraps kapps_ogm.OGM access behind the transitional_sync_middleware Connector protocol.

All middleware graph access goes through that one abstraction. Neither workflow
registration code nor state registration code calls ogm.fetch or ogm.commit directly.
One seam means the graph can be swapped, traced or stubbed in one place.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Optional

import anyio

if TYPE_CHECKING:
    from kapps_triplestore_interface import IRI
    from kapps_ogm import OGM


class KnowledgeGraphConnector:
    """
    A Connector that provides synchronized access to a single entity in the knowledge graph.

    This connector wraps OGM fetch/commit operations behind the transitional_sync_middleware Connector
    protocol, which enables uniform treatment of knowledge graph synchronization alongside
    OT device connectors (MQTT, OPC UA, HTTP).
    """

    def __init__(
        self,
        ogm: "OGM",
        instance_iri: "IRI",
        *,
        class_scope: Optional[Any] = None,
        class_spec: Optional[Any] = None,
        materialize: bool = True,
        named_graph: Optional[str] = None,
    ) -> None:
        """
        Initialize the Knowledge Graph Connector.

        Args:
            ogm: The OGM instance for graph operations.
            instance_iri: The IRI of the graph entity this connector syncs.
            class_scope: Pass through to OGM.fetch for selective hydration.
            class_spec: Pass through to OGM.fetch for class specification.
            materialize: Whether to materialize the fetched node (default True).
            named_graph: The named graph to commit changes to. Pass to OGM.commit.
        """
        self.ogm = ogm
        self.instance_iri = instance_iri
        self.class_scope = class_scope
        self.class_spec = class_spec
        self.materialize = materialize
        self.named_graph = named_graph

    async def connect(self) -> None:
        """
        Establish a connection to the knowledge graph backend.

        This is a lightweight liveness check. The OGM and GraphDB own their own
        connection lifecycle. We do not want to invent GraphDB methods that may not
        exist. This operation is a no-op. It returns immediately.

        Raises:
            ConnectionError: If the backend is not reachable (currently never raised).
        """
        # No-op: the OGM manages its own connection lifecycle.
        # Future: could add a cheap reachability probe if GraphDB exposes one.
        return

    async def disconnect(self) -> None:
        """
        Disconnect from the knowledge graph backend.

        This is a no-op since the OGM/GraphDB owns its own connection lifecycle.

        Raises:
            ConnectionError: If disconnection fails (currently never raised).
        """
        # No-op: the OGM manages its own connection lifecycle.
        return

    async def provide(self) -> Any:
        """
        Fetch the current state of the graph entity.

        Run OGM.fetch off the event loop via anyio.to_thread.run_sync to avoid
        blocking async execution.

        Returns:
            The fetched Node from the knowledge graph.

        Raises:
            ConnectionError: If fetching the data from the knowledge graph fails.
        """
        try:
            fetch_call = partial(
                self.ogm.fetch,
                instance_iri=self.instance_iri,
                class_spec=self.class_spec,
                class_scope=self.class_scope,
                materialize=self.materialize,
            )
            return await anyio.to_thread.run_sync(fetch_call)
        except Exception as exc:
            raise ConnectionError(
                f"Failed to fetch instance {self.instance_iri} from the knowledge graph"
            ) from exc

    async def consume(self, body: Any) -> None:
        """
        Commit updated data to the graph entity.

        Run OGM.commit off the event loop via anyio.to_thread.run_sync to avoid
        blocking async execution.

        Args:
            body: The data dictionary to commit to the knowledge graph.

        Raises:
            ConnectionError: If committing the data to the knowledge graph fails.
        """
        try:
            commit_call = partial(
                self.ogm.commit,
                instance_iri=self.instance_iri,
                data=body,
                named_graph=self.named_graph,
            )
            await anyio.to_thread.run_sync(commit_call)
        except Exception as exc:
            raise ConnectionError(
                f"Failed to commit instance {self.instance_iri} to the knowledge graph"
            ) from exc
