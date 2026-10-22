"""The shipped demo stack refuses a write that violates a shape in its SHACL shapes graph.

`examples/docker/` is the stack a new user starts first: a compose file that runs GraphDB and
creates the repository `kapps-demo` from `graphdb-repo-config.ttl`. That configuration puts a SHACL
layer over the store, turns validation on and names the graph that the shapes are read from. This
module starts that stack, posts one shape into that graph, and makes two writes: one that conforms,
which the store admits, and one that violates the shape, which the store refuses with a validation
report and does not commit.

The stack under test is the shipped one: the shipped compose file, which creates the repository
from the shipped configuration. An override changes only what would collide with another stack on
the same machine. The stack runs as a compose project of its own, so its containers and its volume
are its own, and its GraphDB listens on 127.0.0.1, on a port that Docker picks, not on the shipped
fixed port. `down --volumes` removes the project at the end, also when an assertion fails or the
stack does not come up. A demo stack that runs beside it is not touched.

Opt-in: the test runs only when KAPPS_COMPOSE_COMMAND names a Compose v2 command, such as
`docker compose` or the path of a standalone binary. Without the variable it skips and says so, so
a plain `uv run pytest` never starts Docker. It is also marked `live`, the tier that needs a
GraphDB, so `-m "not live"` leaves it out. It reads no GRAPHDB_* variable: the GraphDB is the one
that it starts. It skips, and says what is missing, where Docker or the compose command does not
run, and where that command does not apply the override's `!override` tag, which an older Compose
v2 ignores or rejects. Run it alone with:

    KAPPS_COMPOSE_COMMAND="docker compose" \\
        uv run pytest -m live tests/test_shipped_stack_validates_shapes.py
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
import time
import uuid
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from rdflib import Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

DOCKER_DIR = Path(__file__).resolve().parents[1] / "examples" / "docker"
COMPOSE_FILE = DOCKER_DIR / "docker-compose.yml"
REPOSITORY_CONFIG = DOCKER_DIR / "graphdb-repo-config.ttl"

COMPOSE_COMMAND_VARIABLE = "KAPPS_COMPOSE_COMMAND"
OPT_IN = (
    f"opt-in: set {COMPOSE_COMMAND_VARIABLE} to a Compose v2 command, e.g. `docker compose`, to "
    "start the shipped stack"
)

# The services of the shipped compose file, and the port GraphDB listens on in its container.
GRAPHDB_SERVICE = "graphdb"
INIT_SERVICE = "graphdb-init"
GRAPHDB_PORT = 7200

STARTUP_TIMEOUT = 240.0
"""Seconds from `up` to a repository that answers. GraphDB starts in well under a minute; the
image pull happens inside `up`, which has a bound of its own."""

# The store states in the media type that its answer is a SHACL validation report, and after the
# `+` the RDF format of the report. The middleware reads a report as N-Quads.
REPORT_MEDIA_TYPE = "application/shacl-validation-report"
NQUADS_REPORT_MEDIA_TYPE = f"{REPORT_MEDIA_TYPE}+n-quads"

REP = Namespace("http://www.openrdf.org/config/repository#")
SHACL_CONFIG = Namespace("http://rdf4j.org/config/sail/shacl#")
EX = Namespace("http://example.org/shipped-stack#")

# A Thing has a label. The conforming write gives one, the violating write gives none.
SHAPES = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX.ThingShape}> a sh:NodeShape ;
    sh:targetClass <{EX.Thing}> ;
    sh:property <{EX.LabelShape}> .
<{EX.LabelShape}> a sh:PropertyShape ;
    sh:path <{EX.label}> ;
    sh:minCount 1 .
"""


@dataclass(frozen=True)
class DemoRepository:
    """The repository that the shipped configuration creates, in the stack that the test runs."""

    base_url: str
    name: str
    shapes_graph: URIRef

    def post_shapes(self, turtle: str) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}/repositories/{self.name}/rdf-graphs/service",
            params={"graph": str(self.shapes_graph)},
            content=turtle,
            headers={"Content-Type": "text/turtle"},
            timeout=30,
        )

    def update(self, sparql: str) -> httpx.Response:
        """Send `sparql` with the headers of the middleware's own updates, which the triplestore
        interface sends; the format of a refusal's report may depend on `Accept`."""
        return httpx.post(
            f"{self.base_url}/repositories/{self.name}/statements",
            content=sparql,
            headers={
                "Content-Type": "application/sparql-update",
                "Accept": "application/sparql-results+json",
            },
            timeout=30,
        )

    def holds(self, subject: URIRef) -> bool:
        response = httpx.post(
            f"{self.base_url}/repositories/{self.name}",
            content=f"ASK {{ <{subject}> ?p ?o }}",
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
            timeout=30,
        )
        response.raise_for_status()
        return bool(response.json()["boolean"])


def _violations(report_text: str) -> set[tuple[Node | None, ...]]:
    """Each result of a report that does not conform: its focus node, path, source shape and
    constraint component. The report is N-Quads, as GraphDB sends it."""
    report = Dataset(default_union=True)
    report.parse(data=report_text, format="nquads")
    (validation_report,) = report.subjects(RDF.type, SH.ValidationReport)
    assert report.value(validation_report, SH.conforms) == Literal(False)
    return {
        (
            report.value(result, SH.focusNode),
            report.value(result, SH.resultPath),
            report.value(result, SH.sourceShape),
            report.value(result, SH.sourceConstraintComponent),
        )
        for result in report.objects(validation_report, SH.result)
    }


def _configured() -> tuple[str, URIRef]:
    """The repository that the shipped configuration creates, and the graph it reads shapes from.

    Read out of the configuration rather than restated, so the shape goes where the shipped
    configuration says the shapes are.
    """
    config = Graph().parse(REPOSITORY_CONFIG, format="turtle")
    (repository,) = config.objects(None, REP.repositoryID)
    (shapes_graph,) = config.objects(None, SHACL_CONFIG.shapesGraph)
    return str(repository), URIRef(str(shapes_graph))


def _compose_command() -> list[str] | None:
    """The compose command that the variable names, or None when the test is not opted into."""
    value = os.environ.get(COMPOSE_COMMAND_VARIABLE, "").strip()
    if not value:
        return None
    if os.name == "nt":
        # POSIX splitting would remove the backslashes of a Windows path.
        return [part.strip('"') for part in shlex.split(value, posix=False)]
    return shlex.split(value)


def _failure(command: list[str], says: str = "") -> str | None:
    """Why `command` failed, in one line, or None when it ran and its output contains `says`."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "no answer within 60 s"
    if result.returncode == 0 and says in result.stdout.lower():
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[0] if lines else f"exit status {result.returncode}"


def _missing(compose: list[str] | None) -> str | None:
    """Why the shipped stack does not start here, or None when it does."""
    if compose is None:
        return OPT_IN
    shown = shlex.join(compose)
    hint = (
        f"set {COMPOSE_COMMAND_VARIABLE} to a Compose v2 command, such as the path of the "
        "standalone docker-compose binary"
    )
    if shutil.which(compose[0]) is None:
        return f"the compose command {shown!r} is not installed; {hint}"
    if shutil.which("docker") is None:
        return "Docker is not installed, and the shipped stack runs on Docker"
    if failure := _failure([*compose, "version"], says="compose"):
        return f"the compose command {shown!r} does not run ({failure}); {hint}"
    if failure := _failure(["docker", "info"]):
        return f"the Docker daemon is not reachable (docker info: {failure})"
    return None


def _override(project: str) -> str:
    """What the test changes of the shipped compose file: the container names and the host port.

    The shipped file fixes both container names, and it publishes GraphDB on 7200. Here the
    containers are named after the project, and GraphDB is published on 127.0.0.1 on a port that
    Docker picks. `!override` replaces the shipped list of ports; without it, the list of this
    file would be added to the shipped one.
    """
    return textwrap.dedent(
        f"""\
        services:
          {GRAPHDB_SERVICE}:
            container_name: {project}-graphdb
            ports: !override
              - "127.0.0.1::{GRAPHDB_PORT}"
          {INIT_SERVICE}:
            container_name: {project}-graphdb-init
        """
    )


def _merged_ports(merged_config: str) -> list[tuple[str, str]]:
    """The host address and host port of each port of GraphDB in the answer of `compose config
    --format json`; an empty host port is one that Docker picks."""
    service = json.loads(merged_config)["services"][GRAPHDB_SERVICE]
    return [
        (str(port.get("host_ip") or ""), str(port.get("published") or ""))
        for port in service.get("ports", [])
    ]


def _run_or_fail(command: list[str], timeout: float) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        pytest.fail(
            f"{shlex.join(command)} failed with exit status {result.returncode}:\n"
            f"{result.stderr or result.stdout}"
        )
    return result.stdout


def _published_port(port_output: str) -> int:
    """The host port in the answer of `compose port`, such as `127.0.0.1:49153`."""
    return int(port_output.strip().rsplit(":", 1)[1])


def _wait_for_repository(base_url: str, repository: str, compose: list[str]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/repositories/{repository}/size", timeout=5).is_success:
                return
        except httpx.TransportError:
            pass
        time.sleep(2)
    logs = subprocess.run(
        [*compose, "logs", "--no-color", "--tail", "40"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    pytest.fail(
        f"the repository {repository!r} did not answer within {STARTUP_TIMEOUT:.0f} s. "
        f"The stack's logs:\n{logs.stdout}{logs.stderr}"
    )


def _down(compose: list[str], project: str) -> None:
    """Remove the project, its containers and its volume. A failure warns and does not raise, so
    it does not hide the outcome of the test."""
    command = [*compose, "down", "--volumes", "--remove-orphans"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        warnings.warn(f"{shlex.join(command)} did not end within 300 s; remove {project} by hand")
        return
    if result.returncode != 0:
        warnings.warn(
            f"{shlex.join(command)} failed; remove {project} by hand:\n{result.stderr}"
        )


@pytest.fixture(scope="module")
def shipped_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The shipped stack, up, as a compose project of its own: its GraphDB's URL."""
    compose = _compose_command()
    if missing := _missing(compose):
        pytest.skip(missing)
    assert compose is not None  # _missing() returned the opt-in reason for None
    repository, _ = _configured()
    project = f"kapps-shipped-shacl-{uuid.uuid4().hex[:8]}"
    override = tmp_path_factory.mktemp("compose") / "override.yml"
    override.write_text(_override(project), encoding="utf-8")
    # The shipped file comes first, so its directory is the project directory and its relative
    # mount of the repository configuration resolves to the shipped configuration.
    in_project = [
        *compose,
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
        "--file",
        str(override),
    ]
    # Before anything starts: a Compose that ignores `!override` adds the shipped fixed port.
    ports = _merged_ports(
        _run_or_fail([*in_project, "config", "--format", "json"], timeout=60)
    )
    if ports != [("127.0.0.1", "")]:
        pytest.skip(
            f"the compose command {shlex.join(compose)!r} does not apply the override's "
            f"`!override` tag (GraphDB's ports: {ports}); a newer Compose v2 does"
        )
    try:
        _run_or_fail([*in_project, "up", "--detach"], timeout=900)
        port = _published_port(
            _run_or_fail([*in_project, "port", GRAPHDB_SERVICE, str(GRAPHDB_PORT)], timeout=60)
        )
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_repository(base_url, repository, in_project)
        yield base_url
    finally:
        _down(in_project, project)


@pytest.fixture(scope="module")
def demo_repository(shipped_stack: str) -> DemoRepository:
    """The demo repository of the shipped stack, with one shape in its shapes graph."""
    repository, shapes_graph = _configured()
    demo = DemoRepository(shipped_stack, repository, shapes_graph)
    posted = demo.post_shapes(SHAPES)
    assert posted.is_success, (
        f"the shapes graph {shapes_graph} did not take the shape: "
        f"{posted.status_code} {posted.text}"
    )
    return demo


@pytest.mark.live
def test_a_write_that_conforms_to_the_shape_is_admitted(demo_repository: DemoRepository):
    admitted = demo_repository.update(
        f'INSERT DATA {{ <{EX.conforming}> a <{EX.Thing}> ; <{EX.label}> "a label" . }}'
    )

    assert admitted.is_success, f"{admitted.status_code} {admitted.text}"
    assert demo_repository.holds(EX.conforming)


@pytest.mark.live
def test_a_write_that_violates_the_shape_is_refused_with_a_validation_report(
    demo_repository: DemoRepository,
):
    refused = demo_repository.update(f"INSERT DATA {{ <{EX.violating}> a <{EX.Thing}> . }}")

    assert refused.is_error, (
        f"the store admitted a Thing without a label ({refused.status_code}): "
        "the shapes graph is not validated as configured"
    )
    media_type = refused.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    assert media_type.startswith(REPORT_MEDIA_TYPE), (
        f"the refusal is not a validation report ({refused.status_code}, {media_type!r}): "
        f"{refused.text[:2000]}"
    )
    assert media_type == NQUADS_REPORT_MEDIA_TYPE, (
        f"the report is {media_type!r}, and the middleware reads a report as N-Quads"
    )
    assert (EX.violating, EX.label, EX.LabelShape, SH.MinCountConstraintComponent) in (
        _violations(refused.text)
    )
    assert not demo_repository.holds(EX.violating), "the refused write was committed"


def test_without_the_variable_the_stack_test_is_not_opted_into(monkeypatch):
    """Offline: no variable, no compose command, and the reason says how to opt in."""
    monkeypatch.delenv(COMPOSE_COMMAND_VARIABLE, raising=False)

    assert _compose_command() is None
    assert _missing(None) == OPT_IN


def test_with_a_missing_compose_command_the_stack_test_skips_and_says_why():
    """Offline: the reason names the command, and the variable that sets another one."""
    reason = _missing(["/nonexistent/docker-compose"])

    assert reason is not None
    assert "/nonexistent/docker-compose" in reason
    assert COMPOSE_COMMAND_VARIABLE in reason
