"""This package contains the knowledge-graph connector and the semantic-connector seam.

When you import this package, it **registers the built-in binding descriptors** on
``semantic.default_registry``. That import is necessary. A middleware
constructed with no explicit ``connector_registry`` gets the default one. An empty default
registry means an empty prune set. An empty prune set means the northbound projection removes
nothing, and every served parameter carries its broker address and topics. The
registry tells the projection which properties are southbound. Population before serving is
the requirement. Population before wiring is not enough on its own.
"""

# ADR: 0028

from kapps_semantic_middleware.connectors.knowledge_graph_connector import (
    KnowledgeGraphConnector,
)

# Imported for its registration side effect. `MQTTBinding` and `RESTBinding` are re-exported
# so a caller can reference either directly (to build a restricted registry, or to override
# one of them).
from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.rest_binding import RESTBinding
from kapps_semantic_middleware.connectors.semantic import (
    BindingDescriptor,
    ParameterBinding,
    Registration,
    SemanticConnectorRegistry,
    default_registry,
    semantic_connector,
)

__all__ = [
    "KnowledgeGraphConnector",
    "MQTTBinding",
    "RESTBinding",
    "BindingDescriptor",
    "ParameterBinding",
    "Registration",
    "SemanticConnectorRegistry",
    "default_registry",
    "semantic_connector",
]
