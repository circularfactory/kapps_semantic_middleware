"""KSM's own state-dashboard boxes, registered through the public registry.

A subproject consumes the dashboard through the same public API. That API is
the contract. If it is awkward for a subproject it must be awkward for KSM
first. These five boxes use only the public surface of
``kapps_semantic_middleware.dashboard``. They reach no private hook.

The boxes describe the middleware machinery: what it holds, which services it
exposes, which connectors it wired, and which of its writes the store refused.
They do not describe resource state. That is the monitor job. Keeping the two
apart is a decided boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kapps_semantic_middleware.dashboard import (
    DashboardRegistry,
    escape,
    hint,
    kv_table,
)
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.modes import Mode
from kapps_semantic_middleware.observation import CallError, is_connection_metadata
from kapps_semantic_middleware.validation_report import render_refused_write
from kapps_semantic_middleware.vocabulary import INF


def _scope_html(scope: Any) -> str:
    """Render a ClassScope as the property chains it selects.

    A ClassScope is a nested mapping of IRI to sub-scope, so its repr is a wall of full IRIs
    that gets cut off long before it says anything. The chains are what a reader wants: this
    scope reached `hasConveyorBelt -> hasConveyorSpeed`, and that is the sentence to print.
    """
    if scope is None:
        return "<p>No scope. The instance holds no materialized tree.</p>"

    def chains(node: Any, prefix: Tuple[str, ...] = ()) -> list:
        out = []
        try:
            items = node.items()
        except AttributeError:
            return [prefix] if prefix else []
        if not items:
            return [prefix] if prefix else []
        for key, child in items:
            label = (
                getattr(key, "fragment", None)
                or str(key).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            )
            out.extend(
                chains(child, prefix + (str(label),)) or [prefix + (str(label),)]
            )
        return out

    found = chains(scope)
    if not found:
        return f"<p>{_iri_cell(scope)}</p>"
    items = "".join(
        f"<li><code>{escape(' &rarr; '.join(c))}</code></li>".replace(
            "&amp;rarr;", "&rarr;"
        )
        for c in found
    )
    return f'<ul class="chains">{items}</ul>'


def _lined_cell(value: Any) -> str:
    """Render a mangled ("lined") IRI as its fragment, with the full IRI on hover.

    A generated field name and a generated route segment are both `IRI.lined` forms, which
    are back-resolvable but unreadable: one parameter reads as
    `https_c__s__s_www_d_sfb1574_..._h_hasConveyorSpeed`. `IRI.from_lined` is the exact
    inverse, so the fragment shown here loses nothing that the title attribute does not keep.

    Display only. Code that acts on the value must use the mangled form, which is what the
    datamodel attribute and the REST path actually carry.
    """
    if value is None:
        return "&mdash;"
    text = str(value)
    try:
        return f'<span title="{escape(text)}">{escape(IRI.from_lined(text).fragment or text)}</span>'
    except Exception:
        # A value that is not a lined IRI is shown as it is. The dashboard never fails on
        # an unexpected shape; it shows what it was given.
        return _iri_cell(text)


def _route_cell(route: Any) -> str:
    """Render a generated REST route with each segment demangled, full route on hover.

    A recursive parameter route is one lined IRI per segment, so a route four levels deep
    runs past four hundred characters and pushes the table off the page. Demangled it reads
    as `/TransferUnit/TransferUnit1/hasConveyorBelt/ConveyorBelt1_left/hasConveyorSpeed`.
    """
    if route is None:
        return "&mdash;"
    text = str(route)
    if not text.startswith("/"):
        return escape(text)
    parts = []
    for segment in text.strip("/").split("/"):
        try:
            parts.append(IRI.from_lined(segment).fragment or segment)
        except Exception:
            parts.append(segment)
    return f'<span title="{escape(text)}">{escape("/" + "/".join(parts))}</span>'


def _model_name(carrier: Any) -> str:
    """Name the model a requestBody or a response declares, or an em dash.

    The reference sits at `content[<media type>]["schema"]["$ref"]`, one level below the
    media object. Testing the media object itself for `$ref` never matches, because its only
    key is `schema`, so both columns rendered an em dash for every operation.

    Not every schema is a reference. A workflow returning `Dict[str, str]` gets an inline
    schema with `type` and `title` and no `$ref` at all, and that is the response a reader
    most wants named. Prefer the title, fall back to the type, and give up only when the
    schema says nothing.
    """
    if not isinstance(carrier, dict):
        return "&mdash;"
    content = carrier.get("content")
    if not isinstance(content, dict):
        return "&mdash;"
    for media in content.values():
        if not isinstance(media, dict):
            continue
        schema = media.get("schema")
        if not isinstance(schema, dict):
            continue
        ref = schema.get("$ref")
        if isinstance(ref, str):
            return escape(ref.rsplit("/", 1)[-1] if ref.startswith("#/") else ref)
        title = schema.get("title")
        if isinstance(title, str) and title:
            return escape(title)
        kind = schema.get("type")
        if isinstance(kind, str) and kind:
            return escape(kind)
    return "&mdash;"


def _disclosure(pruned_set: Any, marker: Any) -> str:
    """The collapsed list of protocol properties the projection stripped from a parameter.

    `marker` is the parameter's own `inf:` interface property, taken from the binding
    descriptor that recognised it. It is read from the descriptor rather than guessed out of
    the pruned set: the set is a frozenset, so picking a member by substring gave a label
    that changed between processes, and a shortened IRI is for display only and must not
    carry a decision.

    A parameter no descriptor matched has no protocol marker to name, because nothing
    declares which protocol it speaks. It gets a general label. Its pruned properties are
    listed all the same, which is the case this disclosure exists to make visible.
    """
    if not pruned_set:
        return ""
    if marker is None:
        summary = f"protocol metadata &mdash; {len(pruned_set)} hidden"
    else:
        summary = f"{_iri_cell(marker)} &mdash; {len(pruned_set)} hidden"
    items = "".join(f"<li><code>{escape(p)}</code></li>" for p in sorted(pruned_set))
    return f"<details><summary>{summary}</summary><ul>{items}</ul></details>"


def _marker_of(bindings: Any) -> Any:
    """The interface property the descriptor of the first matching binding declares.

    `None` when no binding matched, or when the descriptor does not carry one.
    """
    for binding in bindings or ():
        descriptor = getattr(binding, "descriptor", None)
        marker = getattr(descriptor, "interface_property", None)
        if marker is not None:
            return marker
    return None


def _iri_cell(value: Any) -> str:
    """Render an IRI as its local name, with the full IRI in the title attribute.

    A box is a grid cell with a hard width, and a column of full IRIs does not fit in one.
    The local name is what tells an operator which belt or barrier a row is about. The full
    IRI stays reachable on hover, so nothing is lost.

    This is display only. Any code that needs to act on the value must take the full IRI,
    never the shortened form shown here.
    """
    if value is None:
        return "&mdash;"
    text = str(value)
    local = text.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or text
    return f'<span title="{escape(text)}">{escape(local)}</span>'


def _scalar(value: Any) -> Any:
    """Unwrap a one-element list, join a longer one, and pass anything else through.

    Parameter metadata arrives as lists: RDF multiplicity means a property can carry several
    values, so `normalize_metadata` never stores a bare scalar. Rendering the list repr puts
    brackets and quotes in front of the operator for no gain.
    """
    if isinstance(value, (list, tuple, frozenset, set)):
        items = [str(v) for v in value]
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        return ", ".join(items)
    return value


def _collect_parameter_nodes(spec: Any, prefix: Tuple[str, ...] = ()) -> list:
    """Collect parameter nodes from a northbound spec.

    A node is a parameter when its own nested spec declares ``INF.accessMode``
    among its properties. That test is the only one that works without a
    query. A non-empty pruned set is not enough: a parameter reached over a
    protocol with no metadata has an empty set and must still be shown.
    """
    out: List[Tuple[str, ...]] = []
    properties = getattr(spec, "properties", None)
    if not properties:
        return out
    for prop_iri, prop_spec in properties.items():
        nested = getattr(prop_spec, "nested", None)
        if nested is None:
            continue
        nested_props = getattr(nested, "properties", None) or {}
        if str(INF.accessMode) in {str(k) for k in nested_props}:
            out.append(prefix + (str(prop_iri),))
            continue
        out.extend(_collect_parameter_nodes(nested, prefix + (str(prop_iri),)))
    return out


def _demangle_fragment(prop: str) -> str:
    """Return the last fragment of a property IRI string."""
    return prop.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or prop


#: What a time cell says when nothing happened yet.
_NOTHING_YET = "none since the instance started"

#: What a last-sync cell says when no value moved yet.
_NO_SYNC_YET = "no sync since the instance started"


def _ago(seconds: float) -> str:
    """A duration as a person reads it: "4 s", "3 min", "2 h", "5 d"."""
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} d"


def _when(moment: Optional[datetime], now: datetime, nothing: str = _NOTHING_YET) -> str:
    """A moment as its age and its time of day in UTC, "4 s ago (14:03:12 UTC)", or ``nothing``.

    The age is computed here, at the render, from a time the record holds: a record holds no age.
    """
    if moment is None:
        return nothing
    clock = moment.astimezone(timezone.utc).strftime("%H:%M:%S")
    return f"{_ago((now - moment).total_seconds())} ago ({clock} UTC)"


def _error_text(error: Optional[CallError], now: datetime, dev: bool) -> str:
    """The last error: its type and when, and its message on a development page only.

    Which part of the error is left out is what its record marks as connection metadata, and no
    list kept here: an error's message names the host, port, topic or URL it could not reach.
    """
    if error is None:
        return _NOTHING_YET
    text = f"{error.type}, {_when(error.time, now)}"
    if dev or not is_connection_metadata(error, "message"):
        text += f": {error.message}"
    return text


def _state(connected: Optional[bool]) -> str:
    """A connector's state, as the framework's calls of its ``connect`` and ``disconnect`` set it."""
    if connected is None:
        return "never connected"
    return "connected" if connected else "not connected"


def _newest(moments: Iterable[Optional[datetime]]) -> Optional[datetime]:
    """The newest of ``moments`` that is known, or ``None``: the last sync of several connectors."""
    known = [m for m in moments if m is not None]
    return max(known) if known else None


def register(registry: DashboardRegistry) -> None:
    """Register KSM's own five boxes on the given registry.

    The registry is per instance, so registration happens per call. Using
    module-level decorators would tie the boxes to one global registry. Each
    instance then registers its own copy of the same closures.

    Args:
        registry: The instance's dashboard registry to attach boxes to.
    """

    @registry.box(id="header", title="Instance", tab="System", refresh_seconds=5)
    def header_box(middleware: Any) -> str:
        """Identity, health, and graph binding in one panel."""

        def attr(name: str, default: Any = None) -> Any:
            """Read one middleware attribute defensively.

            A consumer-mode instance sets ``resource_iri`` and ``service_iri``
            to ``None`` explicitly, and never creates the queue or the tasks.
            Reading through a plain ``getattr`` would raise on the missing
            task attributes. This helper swallows that and returns the default.
            """
            try:
                value = getattr(middleware, name, default)
            except AttributeError:
                return default
            return value

        def task_alive(task: Any) -> str:
            """Render a task's liveness as a human-readable word."""
            if task is None or task.done():
                return "no"
            return "yes"

        # Values go in raw. `kv_table` escapes every cell it renders, so escaping here as
        # well escaped the escape: `None` became the literal text `&mdash;` instead of an em
        # dash, and an IRI holding `&` showed `&amp;`. One escape, at the render boundary.
        identity_rows: Sequence[Tuple[str, Any]] = [
            ("Resource", attr("resource_iri")),
            ("Service", attr("service_iri")),
            ("Mode", attr("mode")),
            ("Address", attr("address")),
        ]

        # No heartbeat timestamp is held in memory. Querying the graph for
        # ``svc:hasLastHeartbeat`` would be traffic the page causes, which this
        # dashboard forbids. Show the configured interval and liveness instead,
        # and label the row so a reader does not mistake it for an observed age.
        queue = attr("_operation_queue", None)
        heartbeat_live = task_alive(attr("_heartbeat_task", None))
        # Only a watchdog-mode instance runs the sweep. Anywhere else its rows would show an
        # interval that nothing uses and "Sweep task alive: no", which reads like a failure.
        sweep_rows: List[Tuple[str, Any]] = []
        if attr("mode") == Mode.WATCHDOG:
            sweep_rows = [
                ("Sweep interval (s)", attr("sweep_interval")),
                ("Sweep task alive", task_alive(attr("_sweep_task", None))),
            ]
        health_rows: Sequence[Tuple[str, Any]] = [
            ("Heartbeat interval (s)", attr("heartbeat_interval")),
            # The threshold is what a reader judges the heartbeat against, so it belongs
            # beside it even though the observed age cannot be shown without a graph read.
            ("Staleness threshold (s)", attr("staleness_threshold")),
            ("Heartbeat task alive", heartbeat_live),
            *sweep_rows,
            # `None` rather than a literal dash: `kv_table` renders an absent value as the
            # em dash itself, so the one place that decision lives is `escape`.
            ("Operation queue depth", len(queue) if queue is not None else None),
        ]

        # The dataset comes from the OGM's store, as the store's contract names it. An
        # instance without an OGM, or a store without a dataset, yields the em dash, not a
        # crash.
        dataset: Any = getattr(getattr(attr("ogm"), "db", None), "dataset", None)

        graph_rows: List[Tuple[str, Any]] = [
            ("Dataset", dataset),
            # `None` here is not missing information. The instance writes to the default
            # graph, which is what the demo does. An em dash would read as "unknown".
            (
                "Named graph",
                attr("named_graph")
                if attr("named_graph") is not None
                else "default graph",
            ),
        ]
        # What the instance has seen of the store: the last read and write that it answered,
        # and the last call that did not reach it. A refused write is an answer, and shows in
        # Refused writes. Read from the instance's memory, so the box causes no store call.
        binding = attr("graph_binding")
        if binding is not None:
            now = datetime.now(timezone.utc)
            graph_rows += [
                ("Last read", _when(binding.last_read, now)),
                ("Last write", _when(binding.last_write, now)),
                ("Last error", _error_text(binding.last_error, now, registry.dev)),
            ]

        return (
            "<h3>Identity</h3>"
            + kv_table(identity_rows)
            + "<h3>Health</h3>"
            + kv_table(health_rows)
            + "<h3>Graph binding</h3>"
            + kv_table(graph_rows)
        )

    @registry.box(id="held-data", title="Held data", tab="System", refresh_seconds=5)
    def held_data_box(middleware: Any) -> str:
        """Materialized datamodel and per-parameter protocol metadata."""
        plan = getattr(middleware, "_wiring", None)
        if plan is None:
            return "<p>This instance holds no wiring.</p>"

        # Render from the spec plus ``southbound_by_property``, never from
        # the bindings. The pruned set is computed independently of bindings;
        # bindings only add connector and liveness annotations. A parameter
        # whose interface property matches no registered descriptor is absent
        # from bindings while still present in the spec and still pruned.
        # Rendering from bindings hides exactly the case the projection exists
        # to catch.
        rows: list[str] = []

        rows.append("<h3>Instance scope</h3>")
        rows.append(_scope_html(plan.class_scope))

        # No current value is shown in this iteration. Reading the materialized
        # tree out of the TSM persistence needs an ``await``, and a box render
        # is synchronous. That limit is deliberate: a future renderer can
        # switch to an async endpoint without changing what this box means.
        northbound_spec = getattr(plan, "northbound_spec", None)
        if northbound_spec is None:
            rows.append("<h3>Interface-accessible parameters</h3>")
            rows.append("<p>None recognized.</p>")
        else:
            parameter_nodes = _collect_parameter_nodes(northbound_spec)
            if not parameter_nodes:
                rows.append("<h3>Interface-accessible parameters</h3>")
                rows.append("<p>No parameter found in the northbound spec.</p>")
            else:
                bindings_by_property: dict = {}
                for binding in getattr(plan, "bindings", []) or []:
                    key = str(binding.parameter_property)
                    bindings_by_property.setdefault(key, []).append(binding)

                # A binding may have several connectors, one for each direction. Its value's
                # state is theirs together, and its last sync the newest of theirs.
                records_by_value: Dict[Tuple[str, str], list] = {}
                for record in getattr(middleware, "connections", None) or []:
                    value_key = (str(record.holder), str(record.field))
                    records_by_value.setdefault(value_key, []).append(record)
                now = datetime.now(timezone.utc)

                rows.append("<h3>Interface-accessible parameters</h3>")

                for param_chain in parameter_nodes:
                    prop = param_chain[-1]
                    southbound = getattr(plan, "southbound_by_property", {}) or {}
                    pruned_set = southbound.get(prop, frozenset()) or frozenset()
                    pruned_count = len(pruned_set)
                    display_chain = " → ".join(
                        _demangle_fragment(p) for p in param_chain
                    )
                    rows.append(
                        f"<h4><code>{escape(display_chain)}</code> — "
                        f"{pruned_count} pruned</h4>"
                    )

                    matching = bindings_by_property.get(prop, [])
                    if not matching:
                        rows.append(
                            "<p>No binding recognized. The parameter is declared and its "
                            "protocol metadata is pruned, but no registered binding descriptor "
                            "matched its interface property, so no connector serves it and it "
                            "has no northbound route. The properties below were stripped from "
                            "the served datamodel all the same.</p>"
                        )
                        # The disclosure belongs on this path too. A parameter no descriptor
                        # matched is exactly the case the northbound projection exists to
                        # catch, and showing only a count would hide what was stripped.
                        if registry.dev:
                            rows.append(_disclosure(pruned_set, None))
                        continue

                    binding_rows = []
                    for binding in matching:
                        prop_str = str(binding.parameter_property)
                        metadata = binding.metadata or {}

                        topic = metadata.get(str(INF.hasMQTTTopic))
                        if topic is None:
                            for key, value in metadata.items():
                                local = key.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
                                if "MQTTTopic" in local:
                                    topic = value
                                    break
                        topic_html = (
                            escape(_scalar(topic)) if topic is not None else "&mdash;"
                        )
                        topic_cell = f"<td>{topic_html}</td>" if registry.dev else ""

                        records = records_by_value.get(
                            (str(binding.resource_iri), str(binding.field_id)), []
                        )
                        if records:
                            state_text = ", ".join(
                                sorted({_state(r.connected) for r in records})
                            )
                            sync_text: Optional[str] = _when(
                                _newest(r.last_sync for r in records), now, _NO_SYNC_YET
                            )
                        else:
                            # No connector serves the value: the instance wires none for it.
                            state_text = "not wired"
                            sync_text = None

                        routes = getattr(middleware, "_parameter_routes", None) or []
                        resource_lined = (
                            binding.resource_iri.lined
                            if hasattr(binding.resource_iri, "lined")
                            else str(binding.resource_iri)
                        )
                        route = "&mdash;"
                        for candidate in routes:
                            if (
                                candidate.endswith(binding.field_id)
                                and resource_lined in candidate
                            ):
                                route = _route_cell(candidate)
                                break

                        binding_rows.append(
                            "<tr>"
                            f"<td>{_iri_cell(binding.resource_iri)}</td>"
                            f"<td>{_iri_cell(prop_str)}</td>"
                            f"<td>{_lined_cell(binding.field_id)}</td>"
                            f"<td>{escape(binding.access_mode)}</td>"
                            f"{topic_cell}"
                            f"<td>{route}</td>"
                            f"<td>{escape(state_text)}</td>"
                            f"<td>{escape(sync_text)}</td>"
                            "</tr>"
                        )

                    topic_head = "<th>MQTT topic</th>" if registry.dev else ""
                    rows.append(
                        '<div class="table-scroll"><table>'
                        "<thead><tr><th>Holder</th><th>Property</th><th>Field</th>"
                        f"<th>Access</th>{topic_head}<th>Northbound route</th>"
                        "<th>State</th><th>Last sync</th></tr></thead>"
                        f"<tbody>{''.join(binding_rows)}</tbody></table></div>"
                    )

                    # Which properties the projection stripped is connection metadata too: the
                    # count in the heading stays, and the list is shown on a development page
                    # only.
                    if registry.dev:
                        rows.append(_disclosure(pruned_set, _marker_of(matching)))

        return "".join(rows)

    @registry.box(id="services", title="Services", tab="System", refresh_seconds=30)
    def services_box(middleware: Any) -> str:
        """Local workflows and state properties, from the OpenAPI spec.

        The middleware describes the routes that run a workflow, and a state property's route,
        with the registered function's docstring, so each shows its summary and description.
        """
        # ``middleware.app.openapi()`` is generated in process. No traffic.
        # Some app states make it raise, so call it defensively.
        try:
            spec = middleware.app.openapi()
        except Exception:
            return "<p>OpenAPI spec unavailable.</p>"

        paths = spec.get("paths", {})

        workflows: list[str] = []
        state_props: list[str] = []

        for path, methods in paths.items():
            if path.startswith("/workflows/"):
                target = workflows
            elif path.startswith("/state/"):
                target = state_props
            else:
                continue

            for method, op in methods.items():
                if not isinstance(op, dict):
                    continue
                summary = op.get("summary", "")
                description = op.get("description", "")

                request_model = _model_name(op.get("requestBody"))

                # Read the success response, never the first one that happens to carry a
                # reference. FastAPI gives every operation a 422 whose schema is a `$ref` to
                # `HTTPValidationError`, so a loop over all of them reports the generic error
                # for every workflow and never the thing the workflow returns.
                response_model = "&mdash;"
                responses = op.get("responses", {})
                if isinstance(responses, dict):
                    codes = [c for c in responses if str(c).startswith("2")]
                    for code in sorted(codes) or []:
                        named = _model_name(responses.get(code))
                        if named != "&mdash;":
                            response_model = named
                            break

                block = (
                    f"<h4><code>{escape(path)}</code> — {method.upper()}</h4>"
                    f"<p><strong>Summary:</strong> {escape(summary)}</p>"
                    f"<p><strong>Description:</strong> {escape(description)}</p>"
                    f"<p><strong>Request model:</strong> <code>{request_model}</code></p>"
                    f"<p><strong>Response model:</strong> <code>{response_model}</code></p>"
                )
                target.append(block)

        # Remote peers are not in this iteration. Their signatures come from a
        # peer ``/openapi.json`` fetched once at connector wiring and cached.
        # No such cache exists yet. Fetching here would violate the no-traffic
        # rule.
        body = ""
        if workflows:
            body += "<h3>Workflows</h3>" + "".join(workflows)
        else:
            body += "<h3>Workflows</h3><p>None exposed.</p>"
        if state_props:
            body += "<h3>State properties</h3>" + "".join(state_props)
        else:
            body += "<h3>State properties</h3><p>None exposed.</p>"
        return body

    @registry.box(id="connectors", title="Connectors", tab="System", refresh_seconds=10)
    def connectors_box(middleware: Any) -> str:
        """Descriptor-level facts about wired connectors."""
        plan = getattr(middleware, "_wiring", None)
        if plan is None or not plan.registrations:
            return "<p>No connectors wired.</p>"

        # `connections` holds one record per wired connector, in the order of the wiring, so
        # the n-th record is the n-th registration's. A middleware without the records, or with
        # another count, shows no live state rather than a wrong one.
        records: list = list(getattr(middleware, "connections", None) or [])
        if len(records) != len(plan.registrations):
            records = [None] * len(plan.registrations)
        now = datetime.now(timezone.utc)

        rows = []
        for (binding, registration), record in zip(plan.registrations, records):
            suffix = registration.suffix if registration.suffix else "—"
            if record is None:
                live = "<td>&mdash;</td><td>&mdash;</td><td>&mdash;</td>"
            else:
                sync = _when(record.last_sync, now, _NO_SYNC_YET)
                error = _error_text(record.last_error, now, registry.dev)
                live = (
                    f"<td>{escape(_state(record.connected))}</td>"
                    f"<td>{escape(sync)}</td>"
                    f"<td>{escape(error)}</td>"
                )
            rows.append(
                "<tr>"
                f"<td>{_iri_cell(binding.resource_iri)}</td>"
                f"<td>{_lined_cell(binding.field_id)}</td>"
                f"<td>{escape(type(registration.connector).__name__)}</td>"
                f"<td>{escape(registration.sync_direction)}</td>"
                f"<td>{escape(registration.sync_role)}</td>"
                f"<td>{escape(suffix)}</td>"
                f"{live}"
                "</tr>"
            )

        # The last sync is when a value last moved: the last value received, or the last one
        # written. Not the last provide: a pushing connector answers that from a value it
        # holds, so it says nothing of the value's age.
        return (
            hint(
                "State, last sync and last error are what this instance has seen since it "
                "started. The last sync is the last value received or written."
            )
            + '<div class="table-scroll"><table>'
            "<thead><tr><th>Holder</th><th>Field</th><th>Connector class</th>"
            "<th>Sync direction</th><th>Sync role</th><th>Suffix</th>"
            "<th>State</th><th>Last sync</th><th>Last error</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )

    @registry.box(
        id="refused-writes", title="Refused writes", tab="System", refresh_seconds=5
    )
    def refused_writes_box(middleware: Any) -> str:
        """The writes of this instance that the store's shapes refused, newest first.

        Read from the instance's memory, ``middleware.refused_writes``, which ``commit``,
        ``create`` and ``handover`` fill. Each refused write is an expander: its first line shows
        what was refused and by which rules, and it opens to the results and the whole report.
        An expander that a reader opened stays open when the box refreshes.
        """
        refused = getattr(middleware, "refused_writes", None)
        if refused is None:
            return "<p>This instance keeps no record of refused writes.</p>"
        if not refused:
            return "<p>No write of this instance was refused since it started.</p>"
        return hint(
            "Writes of this instance that the store's shapes refused, newest first; the instance "
            "keeps only the newest. Open one to read its results and the store's whole report. "
            "Names are local; hold the pointer over one to read it in full."
        ) + "".join(render_refused_write(each) for each in refused)
