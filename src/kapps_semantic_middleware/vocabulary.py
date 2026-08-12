"""Shared IRI vocabulary for the KAPPS Semantic Middleware.

This module is the single source of truth for the ontology terms the middleware reads and writes.
Every other module (registration, execution, connectors, tests, notebooks) imports its IRIs from
here. The Python code and the published ontologies (Core `cfc:`, the project `svc:` module, and
the domain `mes:` module) can never drift apart.

Four namespaces appear below: `cfc:`, `svc:`, `mes:`, and `inf:`.

`cfc:` is the published Circular Factory Core ontology
(https://w3id.org/circularfactory/Core#). Term names below are verified against the 0.9.0
release.

`svc:` is this project's own Service ontology module
(https://w3id.org/circularfactory/Service#), defined in
`kapps_semantic_middleware/ontology/service.ttl`.

`mes:` is the Manufacturing Execution System domain ontology
(https://w3id.org/circularfactory/MES#), defined in `kapps_semantic_middleware/ontology/mes.ttl`.
It is domain-facing. It covers possession and handover-ability vocabulary.

`inf:` is the interface vocabulary. It defines what makes a domain parameter reachable
over a protocol. It is authored for now under the existing CrcInterfaces IRI, so scenario
3 stays vocabulary-compatible with the minimal example shared across `kapps_triplestore_interface`
and `kapps_ogm`. CrcInterfaces is deprecated. The consolidation capstone (#39) re-homes
these terms under the `inf:` name it mints. Code must therefore reach these terms only
through `class INF`, never inline at a use site. A rename is then one constant
here, plus a find-and-replace.
"""

# ADR: 0012, 0021

from __future__ import annotations

from kapps_triplestore_interface import IRI

CORE_NS = "https://w3id.org/circularfactory/Core#"
SVC_NS = "https://w3id.org/circularfactory/Service#"
MES_NS = "https://w3id.org/circularfactory/MES#"
INF_NS = "https://www.sfb1574.kit.edu/ontologies/CrcInterfaces#"

# Ontology document IRIs (no fragment) — used for owl:imports and named-graph loading.
CORE_ONTOLOGY = IRI("https://w3id.org/circularfactory/Core")
SVC_ONTOLOGY = IRI("https://w3id.org/circularfactory/Service")
MES_ONTOLOGY = IRI("https://w3id.org/circularfactory/MES")

# The three W3C namespaces. Here for the same reason as everything else in this file: ADR 0021
# wants one home for every ontology IRI, and these were previously spelled out at five separate
# use sites across the library and the demo.
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
OWL_NS = "http://www.w3.org/2002/07/owl#"


class RDFS:
    """The `rdfs:` terms this middleware writes into a query or reads out of one."""

    label = IRI("label", base=RDFS_NS)
    subPropertyOf = IRI("subPropertyOf", base=RDFS_NS)
    range = IRI("range", base=RDFS_NS)


META_TYPE_NAMESPACES = (OWL_NS, RDFS_NS, RDF_NS)
"""Namespaces of the types every individual carries that say nothing about what it *is*.

An OGM ``ClassSpec`` resolved against one of these fails outright, and ``owl:NamedIndividual``
in particular is asserted on everything the OGM writes. Anything starting with one of these
is filtered out when the code asks the graph what an individual actually is.
"""


class CFC:
    """Terms from the Core ontology (`cfc:`) that the middleware references."""

    # Classes
    Resource = IRI("Resource", base=CORE_NS)
    Capability = IRI("Capability", base=CORE_NS)
    EquippedCapability = IRI("EquippedCapability", base=CORE_NS)
    FlexibilityCapability = IRI("FlexibilityCapability", base=CORE_NS)
    ChangeabilityCapability = IRI("ChangeabilityCapability", base=CORE_NS)
    Operation = IRI("Operation", base=CORE_NS)
    Task = IRI("Task", base=CORE_NS)
    # Possession (Core reified, time-bound model — verified against Core 0.9.0).
    PossessionState = IRI("PossessionState", base=CORE_NS)  # a Resource-holds-Workpiece state
    Workpiece = IRI("Workpiece", base=CORE_NS)

    # Object properties
    implementsCapability = IRI("implementsCapability", base=CORE_NS)  # Operation -> Capability
    hasCapability = IRI("hasCapability", base=CORE_NS)  # Resource -> Capability
    hasPossessor = IRI("hasPossessor", base=CORE_NS)  # Resource -> PossessionState
    hasPossessedWorkpiece = IRI("hasPossessedWorkpiece", base=CORE_NS)  # Workpiece -> PossessionState


class SVC:
    """Terms from this project Service ontology (`svc:`)."""

    # Classes
    Service = IRI("Service", base=SVC_NS)
    Workflow = IRI("Workflow", base=SVC_NS)
    StateProperty = IRI("StateProperty", base=SVC_NS)

    # Object properties
    hasService = IRI("hasService", base=SVC_NS)  # Resource -> Service
    isServiceOf = IRI("isServiceOf", base=SVC_NS)  # Service -> Resource
    hasWorkflow = IRI("hasWorkflow", base=SVC_NS)  # Service -> Workflow
    isWorkflowOf = IRI("isWorkflowOf", base=SVC_NS)  # Workflow -> Service
    hasStateProperty = IRI("hasStateProperty", base=SVC_NS)  # Service -> StateProperty
    isStatePropertyOf = IRI("isStatePropertyOf", base=SVC_NS)  # StateProperty -> Service
    realizedByWorkflow = IRI("realizedByWorkflow", base=SVC_NS)  # Capability -> Workflow
    realizesCapability = IRI("realizesCapability", base=SVC_NS)  # Workflow -> Capability
    providedByStateProperty = IRI("providedByStateProperty", base=SVC_NS)  # Capability -> StateProperty
    providesCapability = IRI("providesCapability", base=SVC_NS)  # StateProperty -> Capability
    precondition = IRI("precondition", base=SVC_NS)  # Workflow -> (SHACL-described args)
    outcome = IRI("outcome", base=SVC_NS)  # Workflow -> (SHACL-described return)

    # Datatype properties
    address = IRI("address", base=SVC_NS)  # Service -> xsd:anyURI (base URL)
    endpoint = IRI("endpoint", base=SVC_NS)  # Workflow|StateProperty -> xsd:anyURI (full URL)
    lastHeartbeat = IRI("lastHeartbeat", base=SVC_NS)  # Service -> xsd:dateTime

    # Execution provenance (R12, ADR 0009) — written onto a cfc:Operation by the pull-and-run
    # terminal transition. Success is carried by the terminal operationStatus (done/failed).
    operationStatus = IRI("operationStatus", base=SVC_NS)  # Operation -> xsd:string (queued/running/done/failed, ADR 0009)
    executedByWorkflow = IRI("executedByWorkflow", base=SVC_NS)  # Operation -> Workflow
    executionTimestamp = IRI("executionTimestamp", base=SVC_NS)  # Operation -> xsd:dateTime
    executionResult = IRI("executionResult", base=SVC_NS)  # Operation -> xsd:string
    failureState = IRI("failureState", base=SVC_NS)  # Operation -> xsd:string (JSON resource-datamodel dump on failure)


class MES:
    """Terms from this project MES ontology (`mes:`), defined in ontology/mes.ttl.

    Scope is now handover *ability* only. Possession itself is Core material-flow state
    (``cfc:PossessionState`` / ``cfc:hasPossessor`` / ``cfc:hasPossessedWorkpiece``, see
    ``class CFC``). This module no longer mints its own possession vocabulary.
    """

    # Classes
    HandoverAbility = IRI("HandoverAbility", base=MES_NS)  # Enumerated class with six individuals

    # Object properties
    hasHandoverAbility = IRI("hasHandoverAbility", base=MES_NS)  # Resource -> HandoverAbility
    complements = IRI("complements", base=MES_NS)  # HandoverAbility -> HandoverAbility (symmetric)

    # Handover-ability individuals (three complementary pairs)
    Put = IRI("Put", base=MES_NS)  # Source-active giving. Complements Receive.
    Receive = IRI("Receive", base=MES_NS)  # Passive counterpart of Put. Complements Put.
    Pick = IRI("Pick", base=MES_NS)  # Destination-active taking. Complements Release.
    Release = IRI("Release", base=MES_NS)  # Passive counterpart of Pick. Complements Pick.
    Pass = IRI("Pass", base=MES_NS)  # Both-active giving. Complements Retrieve.
    Retrieve = IRI("Retrieve", base=MES_NS)  # Both-active taking. Complements Pass.


class INF:
    """Terms from the interface vocabulary (`inf:`).

    A **parameter** is one node hanging off a domain property. It carries the value together with
    everything needed to reach it over a protocol. The terms split into two layers.
    The split is load-bearing:

    - **Northbound-safe** — declared by the generic marker ``isInterfaceAccessibleParameter``:
      ``accessMode``, and the parameter own domain content. Safe to serve to a peer.
    - **Southbound only** — declared by a protocol marker such as
      ``isInterfaceAccessibleMQTTParameter``: the connection metadata. A peer that learned the
      broker address and topics could drive the device directly. It would bypass the middleware.
      This must never reach a northbound payload.

    The core never decides which terms are southbound by name. A binding descriptor declares its
    own ``connection_metadata``. The registry takes the union.
    """

    # ADR: 0015, 0021, 0028

    # Interface marker properties. A domain property becomes interface-accessible by being
    # rdfs:subPropertyOf one of these. The protocol marker is a subproperty of the generic one.
    # This is what makes the two range restrictions merge into one effective shape.
    isInterfaceAccessibleParameter = IRI("isInterfaceAccessibleParameter", base=INF_NS)
    isInterfaceAccessibleMQTTParameter = IRI(
        "isInterfaceAccessibleMQTTParameter", base=INF_NS
    )

    # Parameter content (northbound-safe).
    hasValue = IRI("hasValue", base=INF_NS)  # the live value; absent under the locator pattern
    accessMode = IRI("accessMode", base=INF_NS)  # "read" | "readwrite" facet

    # MQTT connection metadata (southbound only).
    hasMQTTTopic = IRI("hasMQTTTopic", base=INF_NS)  # topic the device publishes readings on
    hasMQTTSetTopic = IRI("hasMQTTSetTopic", base=INF_NS)  # setpoint topic; readwrite only
    hasMQTTBrokerIP = IRI("hasMQTTBrokerIP", base=INF_NS)  # broker carrying both topics
    hasMQTTBrokerPort = IRI("hasMQTTBrokerPort", base=INF_NS)  # xsd:integer; absent means 1883
    hasMQTTValuePath = IRI("hasMQTTValuePath", base=INF_NS)  # JSON envelope path; absent = raw scalar


class AccessMode:
    """String values for `inf:accessMode`. Not IRIs / individuals.

    Absent or unrecognised means read-only. A parameter is never writable by accident of omission
    by accident of omission.
    """

    READ = "read"
    READWRITE = "readwrite"
    ALL = (READ, READWRITE)


class OperationStatus:
    """String values for svc:operationStatus, in lifecycle order. Not IRIs / individuals."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ALL = (QUEUED, RUNNING, DONE, FAILED)
