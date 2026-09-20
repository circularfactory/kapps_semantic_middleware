"""REST routes, generated recursively to the complex property.

The middleware extends the CRUD API of the resource datamodel with recursive routes. These routes descend
the datamodel tree. They terminate at each interface-accessible parameter. This parameter is a
``PropertyValueKind.COMPLEX`` property. Its blanknode dict is the atomic addressable unit. The
framework generator produces only top-level CRUD. This module adds the parameter routes beneath it.

Recursion terminates at COMPLEX properties. RDF has no properties about properties. Metadata about
a conveyor speed is its unit and its MQTT topic. The middleware models this metadata as a blanknode.
This blanknode hangs off the parameter property. That blanknode materializes as an ``AnonymousClass``
with no ``id``. It is not ``Identifiable``. It is never routable. Treat the blanknode dict as the
atomic element. This approach makes the id-less problem disappear. The **property** is the last
segment. The dict is the body. Value and unit move together. They must move together. A speed
without its unit is not a speed.

Every value in this tree is a **list**. RDF multiplicity requires this (research doc 0029). This
requirement includes scalars. The original example showed a bare dict. That example predates the
finding. The payload is a list of parameter dicts. It is not a bare dict.

Verb gating is per individual. One belt may be ``readwrite``. A barrier on the same unit may be
``read``. Path segments are therefore **literal**. They are not FastAPI path parameters. A
parameterized route could not express per-individual verb differences. A PUT to a read-only
parameter returns 405. The route does not exist. FastAPI handles that automatically.

The request or response type comes from the **owning node** model field annotation. It does not
come from ``binding.node_model_type``. The latter comes from the full spec. It carries southbound
connection metadata. The northbound instance owns its annotation. That annotation is the pruned
shape. Use the wrong one. This mistake leaks broker addresses northbound.
"""

from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any, Dict, List, Sequence, Set, Tuple

from fastapi import APIRouter, Body, HTTPException
from kapps_triplestore_interface import IRI
from pydantic import BaseModel

from transitional_sync_middleware.model.data_model import DataModel
from transitional_sync_middleware.middleware.registries import ConnectionInfo

from kapps_semantic_middleware.connectors.semantic import AccessMode, ParameterBinding

logger = logging.getLogger(__name__)


def _lined(value: Any) -> str:
    """Convert an IRI or plain string into a URL-safe segment.

    This function tolerates a value already an ``IRI`` (has ``lined``). It also tolerates
    one that is a plain ``str``. This function is total and invertible. Raw IRIs contain
    ``/``. They would break routing.
    """
    if hasattr(value, "lined"):
        return value.lined
    return IRI(value).lined


def _owner_of(model: Any, owner_id: str, field_id: str) -> Any:
    """Return the nested individual a parameter route addresses. Return a 500 naming what is absent.

    The `DataModel` rebuilds per request. It does not capture when the route was generated. This
    design is deliberate. The handler must navigate the model persistence holds *now*. A map built
    at generation time would pin objects. Later writes replace those objects. This rebuild is
    the same rebuild `update_persistence_with_value` does. It does so for the same reason.

    `get_model` returns ``None`` when recognition finds a binding. Its owner is not in the
    materialized tree. Ontology drift causes this. A scope that pruned the branch out from under it
    also causes this. Let that reach ``getattr``. This produces ``AttributeError: 'NoneType'``. It
    surfaces as a generic error. It names neither the individual nor the cause. Fail with both.
    """
    owner = DataModel.from_models(model).get_model(owner_id)
    if owner is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"{owner_id} carries the recognized parameter {field_id} but is absent from the "
                f"materialized tree, so the route cannot be served"
            ),
        )
    return owner


def _make_get_handler(
    data_model_name: str,
    root_id: str,
    owner_id: str,
    field_id: str,
    response_annotation: Any,
    middleware: Any,
) -> Any:
    """Build a GET handler for one parameter route.

    This function mirrors ``get_persistence_value`` field-level branch. That branch is from
    ``transitional_sync_middleware/middleware/sync/synchronization.py``. That is what makes the read precise.
    Only the one field returns. Every sibling stays untouched.
    """
    async def get_parameter():
        try:
            connection_info = ConnectionInfo(
                data_model_name=data_model_name, model_id=root_id
            )
            connector = middleware.persistence_registry.get_connection(connection_info)
            model = await connector.provide()

            if owner_id == root_id:
                value = getattr(model, field_id)
            else:
                value = getattr(_owner_of(model, owner_id, field_id), field_id)

            return value
        except HTTPException:
            # Already carries a considered status. Re-raising keeps it. Otherwise every
            # failure below would flatten into a 400.
            raise
        except Exception as e:
            # 500, not 400: the caller asked for a route this middleware generated and
            # advertised, so a failure to serve it is ours. A 400 here would send an operator
            # looking at their own request for a fault that is on this side.
            logger.warning("GET %s failed: %s", field_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    # FastAPI reads `inspect.signature()` before it looks at `__annotations__`. This module
    # has `from __future__ import annotations`. A written-out annotation would reach it as an
    # unresolvable string. Stamp the signature. This drives request parsing.
    get_parameter.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[],
        return_annotation=response_annotation,
    )
    return get_parameter


def _make_put_handler(
    data_model_name: str,
    root_id: str,
    owner_id: str,
    field_id: str,
    body_annotation: Any,
    middleware: Any,
) -> Any:
    """Build a PUT handler for one parameter route.

    This function mirrors ``update_persistence_with_value`` final ``else:`` branch. That branch is
    from ``transitional_sync_middleware/middleware/sync/synchronization.py``. That is what makes the write
    precise. Only the one field mutates. Every sibling stays byte-identical. A downstream
    diff-based commit writes nothing for the siblings. See that file lines around the field-level
    branch.

    The precision has to survive the ``consume`` call as well, and at first it did not. A
    persistence connector is keyed by *(data_model_name, model_id)*, so every connector on
    this resource hangs off the one this handler fetches. Handing it the whole model with no
    further word meant the fan-out asked *every* write leg on the resource to re-derive its
    slice -- including a sibling parameter this PUT never touched, whose write leg then
    published its last *observed* value onto its *set* topic as though someone had commanded
    it. ``changed`` carries the same field identity the mutation above already used, so the
    fan-out reaches this parameter's write leg and no other.
    """
    async def put_parameter(body):
        try:
            connection_info = ConnectionInfo(
                data_model_name=data_model_name, model_id=root_id
            )
            connector = middleware.persistence_registry.get_connection(connection_info)
            model = await connector.provide()

            if owner_id == root_id:
                setattr(model, field_id, body)
            else:
                setattr(_owner_of(model, owner_id, field_id), field_id, body)

            # `contained_model_id=owner_id` unconditionally, including when the parameter
            # sits on the root: `middleware._wire_semantic_connectors` registers every
            # binding with `contained_model_id=str(binding.resource_iri)`, so a root-owned
            # parameter's connector carries the root's own id there rather than `None`.
            # Leaving it `None` here would widen the scope to every field on the resource
            # and quietly restore the defect.
            await connector.consume(
                model,
                changed=ConnectionInfo(
                    data_model_name=data_model_name,
                    model_id=root_id,
                    contained_model_id=owner_id,
                    field_id=field_id,
                ),
            )
            # A PUT is always news. Something northbound chose to act. Log after the
            # consume. The line means "applied" rather than "attempted".
            logger.info("PUT applied: %s on %s = %r", field_id, owner_id, body)
            return {"message": f"Updated {field_id}"}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("PUT %s failed: %s", field_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    # As above: the body type has to arrive as a real object on the signature. Do not use a string
    # annotation. FastAPI would fail to resolve it.
    #
    # `body_annotation` is the owning model's field annotation. For a COMPLEX property that
    # is not required, that annotation is `Optional[Annotated[conlist(Model, ...), BeforeValidator]]`
    # -- not the plain `List[Model]` it is for a required one. FastAPI infers body-vs-query from
    # whether the annotation looks "scalar", and an `Optional` wrapper defeats that inference: it
    # falls back to a query parameter named "body", which every real PUT then 422s against, since
    # a list can never arrive on a query string. Mark it explicitly rather than rely on inference.
    put_parameter.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(
                "body",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Annotated[body_annotation, Body()],
            )
        ],
        return_annotation=Dict[str, str],
    )
    return put_parameter


def _accumulate_routes(
    inst: Any,
    bindings_map: Dict[str, List[ParameterBinding]],
    current_path: List[str],
    visited: Set[str],
) -> List[Tuple[str, ParameterBinding, str, Any]]:
    """Walk the instance. Return (path, binding, owner_id, field_annotation) tuples."""
    results: List[Tuple[str, ParameterBinding, str, Any]] = []

    node_id = getattr(inst, "id", None)
    if node_id is None:
        return results

    node_id_str = str(node_id)
    if node_id_str in visited:
        return results
    # A new set per branch rather than add-then-backtrack. `visited` is scoped to the path
    # taken. It is not scoped to the walk as a whole. An individual genuinely reachable by two
    # routes should be addressable at both. A walk-wide set would silently drop the second. The
    # cost is a set per node over a handful of nodes. This happens once, at startup.
    visited = visited | {node_id_str}

    owner_type = type(inst)

    for binding in bindings_map.get(node_id_str, []):
        param_path = "/".join(current_path + [binding.field_id])
        field_info = owner_type.model_fields.get(binding.field_id)
        field_annotation = field_info.annotation if field_info else Any
        results.append((param_path, binding, node_id_str, field_annotation))

    for field_name in type(inst).model_fields:
        value = getattr(inst, field_name, None)
        if not isinstance(value, list):
            continue

        for element in value:
            # `isinstance` rather than a `model_fields` probe. Pydantic 2.11 deprecates reading
            # that attribute off an instance. It is the wrong question anyway. What matters
            # is that the element is a model we can descend. It is not that it exposes a field map.
            if not isinstance(element, BaseModel):
                continue
            child_id = getattr(element, "id", None)
            if child_id is None:
                continue

            next_path = current_path + [field_name, _lined(str(child_id))]
            results.extend(
                _accumulate_routes(
                    inst=element,
                    bindings_map=bindings_map,
                    current_path=next_path,
                    visited=visited,
                )
            )

    return results


def generate_recursive_rest_api(
    middleware: Any,
    data_model_name: str,
    *,
    root_id: str,
    instance: Any,
    bindings: Sequence[ParameterBinding],
) -> List[str]:
    """Generate the REST surface of the resource middleware. Generate top-level CRUD plus recursive parameter routes.

    Call ``middleware.generate_rest_api_for_data_model`` first. This produces the unchanged top-level
    CRUD. Then walk the instance tree. Mount parameter routes beneath it. Return the list of
    generated parameter route paths. Count the path string per route. Count GET and PUT of the same
    path once. Ordering is tree order. It is depth-first.

    Args:
        middleware: The semantic middleware instance. It provides ``app``, ``persistence_registry``,
            and ``generate_rest_api_for_data_model``.
        data_model_name: The name of the data model for ConnectionInfo.
        root_id: The IRI of the root resource instance.
        instance: The materialized root instance. Walk its tree.
        bindings: The recognized ParameterBindings from WiringPlan.bindings.

    Returns:
        List of generated parameter route paths. Empty if the instance has no id.
    """
    middleware.generate_rest_api_for_data_model(data_model_name)

    root_node_id = getattr(instance, "id", None)
    if root_node_id is None:
        logger.warning(
            "Root instance for %s has no id. Generate top-level CRUD only. Generate no parameter routes.",
            data_model_name,
        )
        return []

    bindings_by_owner: Dict[str, List[ParameterBinding]] = {}
    for binding in bindings:
        owner_key = str(binding.resource_iri)
        bindings_by_owner.setdefault(owner_key, []).append(binding)

    # The leading empty segment puts the "/" on the front. Join these segments later.
    # Starlette asserts `path.startswith("/")`. A missing one is a construction-time crash.
    # It is not a 404. Get this right here. A diagnosis there is hard.
    initial_path = ["", type(instance).__name__, _lined(str(root_node_id))]
    route_specs = _accumulate_routes(
        inst=instance,
        bindings_map=bindings_by_owner,
        current_path=initial_path,
        visited=set(),
    )

    router = APIRouter()

    for path, binding, owner_id, field_annotation in route_specs:
        get_handler = _make_get_handler(
            data_model_name=data_model_name,
            root_id=root_id,
            owner_id=owner_id,
            field_id=binding.field_id,
            response_annotation=field_annotation,
            middleware=middleware,
        )
        router.get(path, response_model=field_annotation)(get_handler)

        if binding.access_mode == AccessMode.READWRITE:
            put_handler = _make_put_handler(
                data_model_name=data_model_name,
                root_id=root_id,
                owner_id=owner_id,
                field_id=binding.field_id,
                body_annotation=field_annotation,
                middleware=middleware,
            )
            router.put(path)(put_handler)

    middleware.app.include_router(router)

    return [spec[0] for spec in route_specs]
