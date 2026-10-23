"""Service identity is per middleware instance. Not per resource.

`mint_service_iri` used to be a pure function of the resource. Two middlewares bound
to one resource shared a single `svc:Service` node. The second registration overwrote
the first `svc:hasAddress`. Both heartbeated the same `svc:hasLastHeartbeat`. Either shutdown
deregistered the survivor. None of it failed loudly. These tests pin the two properties
that fix it. A discriminator **stable across restarts of the same deployment**.
**Distinct between concurrent instances**. The discovery helper replaces reconstructing
a service IRI from a resource IRI.

The unit tests are pure logic. The integration tests need a live store. Skip when
the `TRIPLESTORE_*` env vars are absent.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from kapps_triplestore_interface import IRI

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_env_present, triplestore_for
from kapps_semantic_middleware.registration import (
    mint_service_iri,
    services_of_resource,
    sweep_stale_services,
    update_heartbeat,
)
from kapps_semantic_middleware.vocabulary import SVC

requires_store = pytest.mark.skipif(
    not triplestore_env_present(),
    reason="TRIPLESTORE_* environment variables not set; skipping live-store integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

# Ports nothing else in the suite binds. No server is started here. The middlewares
# register through `_register_service` directly. The port decides the address. The
# address discriminates the service node.
PORT_A = 8971
PORT_B = 8972

RESOURCE = IRI(seed.HELLO_RESOURCE)
ADDR_A = f"http://127.0.0.1:{PORT_A}"
ADDR_B = f"http://127.0.0.1:{PORT_B}"


class TestMintServiceIri:
    """The minted IRI: distinct per instance. Stable per deployment. Still reversible."""

    def test_two_instances_on_one_resource_get_distinct_iris(self):
        assert mint_service_iri(RESOURCE, ADDR_A) != mint_service_iri(RESOURCE, ADDR_B)

    def test_the_same_deployment_reuses_its_iri(self):
        # The restart case. A returning instance must re-adopt its node. Not orphan it.
        assert mint_service_iri(RESOURCE, ADDR_A) == mint_service_iri(RESOURCE, ADDR_A)

    def test_the_iri_is_rooted_at_the_resource(self):
        # Which resource a service belongs to stays readable off the IRI alone. As before.
        assert str(mint_service_iri(RESOURCE, ADDR_A)).startswith(f"{RESOURCE}_service_")

    def test_trivial_address_spellings_collapse_to_one_iri(self):
        # A trailing slash or a differently-cased host is the same deployment. Mint two nodes
        # for it. Resurrect exactly the orphaning this change prevents.
        assert (
            mint_service_iri(RESOURCE, "http://localhost:8000")
            == mint_service_iri(RESOURCE, "http://localhost:8000/")
            == mint_service_iri(RESOURCE, "http://LocalHost:8000")
        )

    def test_the_discriminator_reverses_to_the_address(self):
        # Production IRIs stay back-resolvable. The discriminator is `IRI.lined`
        # not a hash. The instance address is readable off its own node IRI.
        minted = mint_service_iri(RESOURCE, ADDR_A)
        discriminator = str(minted)[len(f"{RESOURCE}_service_") :]
        assert str(IRI.from_lined(discriminator)) == ADDR_A

    def test_an_address_that_is_not_an_absolute_url_is_rejected(self):
        with pytest.raises(ValueError):
            mint_service_iri(RESOURCE, "8000")


def _hello_instance(store, port: int) -> SemanticMiddleware:
    """A resource-mode middleware on the shared hello resource. Distinguished only by port."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=port,
    )


def _two_registered_instances(store) -> tuple[SemanticMiddleware, SemanticMiddleware]:
    """Two instances on one resource. Both registered. The scenario one-Service-per-instance exists for."""
    seed.seed_scenario1(store)
    mw_a = _hello_instance(store, PORT_A)
    mw_b = _hello_instance(store, PORT_B)
    asyncio.run(mw_a._register_service())
    asyncio.run(mw_b._register_service())
    return mw_a, mw_b


@requires_store
class TestTwoInstancesOnOneResource:
    """Two middlewares on one `resource_iri`. A controller and a monitor, in the glossary's terms."""

    def test_each_instance_registers_its_own_service(self, store):
        mw_a, mw_b = _two_registered_instances(store)

        assert mw_a.service_iri != mw_b.service_iri
        for mw in (mw_a, mw_b):
            addresses = store.triples_get(sub=mw.service_iri, pred=SVC.hasAddress)
            assert [str(t[2]) for t in addresses] == [mw.address]
            assert store.triple_exists((mw.service_iri, SVC.isServiceOf, seed.HELLO_RESOURCE))

    def test_heartbeats_are_independent(self, store):
        mw_a, mw_b = _two_registered_instances(store)

        def heartbeats(mw) -> list[str]:
            return [str(t[2]) for t in store.triples_get(sub=mw.service_iri, pred=SVC.hasLastHeartbeat)]

        # Registration writes each Service's first heartbeat with its address.
        assert len(heartbeats(mw_a)) == 1
        assert len(heartbeats(mw_b)) == 1
        b_before = heartbeats(mw_b)

        # A beat of one instance moves its own heartbeat only. A shared node would report the
        # resource alive while *either* process lives, which stops liveness meaning what
        # discovery assumes it means.
        a_later = datetime.now(timezone.utc) + timedelta(minutes=5)
        update_heartbeat(mw_a.ogm, mw_a.service_iri, timestamp=a_later)
        assert len(heartbeats(mw_a)) == 1
        assert heartbeats(mw_a) != b_before
        assert heartbeats(mw_b) == b_before

    def test_stopping_one_leaves_the_other_discoverable_and_alive(self, store):
        mw_a, mw_b = _two_registered_instances(store)
        asyncio.run(mw_b.emit_heartbeat())
        asyncio.run(mw_a._deregister_service())

        assert not store.triples_get(sub=mw_a.service_iri, pred=SVC.hasAddress)
        assert store.triples_get(sub=mw_b.service_iri, pred=SVC.hasAddress)
        reachable = services_of_resource(OGM(db=store), seed.HELLO_RESOURCE, reachable_only=True)
        assert reachable == [mw_b.service_iri]
        # Discoverable is not the same as alive. The survivor must hold its own heartbeat.
        # A shared node could not have expressed this.
        assert len(store.triples_get(sub=mw_b.service_iri, pred=SVC.hasLastHeartbeat)) == 1

    def test_a_restart_reuses_its_node_rather_than_orphaning_one(self, store):
        mw_a, _ = _two_registered_instances(store)
        asyncio.run(mw_a._deregister_service())

        restarted = _hello_instance(store, PORT_A)
        asyncio.run(restarted._register_service())

        assert restarted.service_iri == mw_a.service_iri
        # Two instances ever existed. Two nodes exist. A third would be an orphan.
        assert len(services_of_resource(OGM(db=store), seed.HELLO_RESOURCE)) == 2

    def test_only_the_stale_instance_is_swept(self, store):
        """A crashed instance goes stale on its own. Its sibling keeps reachability.

        What the watchdog does with the resource's Operations once a resource carries several
        Services is decided per Operation; test_liveness_integration covers it.
        """
        mw_live, mw_dead = _two_registered_instances(store)
        ogm = OGM(db=store)
        update_heartbeat(ogm, mw_live.service_iri)
        update_heartbeat(
            ogm, mw_dead.service_iri, timestamp=datetime.now(timezone.utc) - timedelta(hours=1)
        )

        swept = sweep_stale_services(ogm, max_age_seconds=60.0)

        assert mw_dead.service_iri in swept and mw_live.service_iri not in swept
        assert not store.triples_get(sub=mw_dead.service_iri, pred=SVC.hasAddress)
        assert store.triples_get(sub=mw_live.service_iri, pred=SVC.hasAddress)

    def test_services_of_resource_finds_both(self, store):
        mw_a, mw_b = _two_registered_instances(store)

        # Consumers query svc:isServiceOf. Not reconstruct the IRI. Accept more than one answer.
        assert sorted(services_of_resource(OGM(db=store), seed.HELLO_RESOURCE)) == sorted(
            [mw_a.service_iri, mw_b.service_iri]
        )
