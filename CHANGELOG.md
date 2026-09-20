# Changelog

Notable changes to `kapps-semantic-middleware`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

`release-mechanism prepare` turns the Unreleased heading below into the dated entry for the
version being released, and stops the release if there is no such heading. Write the entry as the
work lands, not at release time.

(That sentence deliberately does not spell the heading out. The release mechanism insists on
finding exactly one of it, and prose naming it is a second occurrence — which stopped a release
the first time this file was written.)

## 0.1.1 — 2026-09-20

### Fixed

- The factory demonstration (`kapps-transferunit-factory`) no longer deadlocks on Windows while
  it starts. The launcher now drains every child's output from the moment it spawns the child:
  - a middleware's output no longer waits unread while the launcher waits for the next PLC
  - a PLC's output drains on a thread, not on the launcher's own thread
- Ctrl+C stops the factory demonstration cleanly. Before, a `KeyboardInterrupt` traceback and
  exit code 130 followed "Stopping factory..." after every child had already stopped.
- Scenario 1 labels the `executionTimestamp` it prints as UTC. The graph still stores UTC, as
  before.
- The hint that `kapps-examples` prints names the README section that exists, "Start a GraphDB".
- The README now states three things about the setup:
  - `kapps-examples` does the copying
  - its destination is relative to the current working directory
  - the three `GRAPHDB_*` variables have PowerShell, `cmd.exe` and `setx` forms beside the POSIX
    ones
- The `docker compose` file that `kapps-examples` copies names its project `kapps-demo` and its
  containers `kapps-demo-graphdb` and `kapps-demo-graphdb-init`. Before, compose took the
  project name from the containing directory, which was literally "docker".
- The `docker compose` configuration that `kapps-examples` copies now creates the `kapps-demo`
  repository with the `owl-max` ruleset. `rdfsplus-optimized` did not infer the `rdfs:Class` of
  an `rdfs:range`, which `kapps-ogm` needs. A `kapps-demo` repository that already exists keeps
  its ruleset.
- An install no longer prints a `SyntaxWarning` from `kapps-triplestore-interface`. This package
  now requires at least version 2.1.1 of it, which carries the fix.

### Changed

- The three sibling dependencies are compatible-release ranges now:
  - `kapps-ogm~=0.2.0`
  - `kapps-triplestore-interface~=2.1.1`
  - `transitional-sync-middleware~=0.1.0`

  A patch release of a sibling installs as before. A new minor or major release installs only
  after this package declares it.
- No file in the distribution cites a decision record any more. The wheel excludes those records,
  so each citation pointed at text the user could not read. Every statement that carried a
  citation keeps its content. The sweep covers:
  - the output that scenario 1 prints
  - the `docker compose` file
  - docstrings and source comments
  - `AGENTS.md`, `CONTEXT-MAP.md` and the glossary
  - the examples' documentation
- `AGENTS.md` now says where the reasoning lives. Before, it told a consuming agent not to follow
  the citations.
- No shipped file cites an issue-tracker ticket any more. Each rule that a citation stood for
  now appears in place. The sweep covers:
  - docstrings and notebook code cells
  - the command-line help texts
  - the station board's hover text and the factory demonstration's page templates
  - the factory ontology's comment
  - the source comments that the documentation site renders
  - every other source comment, the tests, the manifest and this changelog
  - the TransferUnit ontology's design notes, folded into the ontology's own comments and
    retired

### Known limitations

- The SHACL validation that the `kapps-demo` repository configures is unverified. No test shows
  yet that the repository rejects a write that violates a shape.

## 0.1.0 — 2026-08-12

### Added

- **First public release.** A resource's state is described in an RDF knowledge graph as a
  protocol-interface parameter, and the middleware wires that description to a real device:
  peer discovery through the graph, a northbound REST surface, and southbound connectors that
  keep graph and device in step.
- Two runnable scenarios and a six-process factory demonstration, copied out of the installed
  package with `kapps-examples` — including the `docker compose` files that stand up the
  GraphDB they need, which previously reached only a reader of the source repository.
- `kapps-transferunit-factory`, a console script that boots the factory demonstration.
- Agent-facing documentation inside the distribution: `AGENTS.md`, `CONTEXT-MAP.md` and the
  eight `docs/mechanics/` pages, so a consuming agent reading `site-packages` finds the rules
  that explain what it is reading.

### Changed

- The synchronization layer is now `transitional-sync-middleware`, imported as
  `transitional_sync_middleware`. It is the same code this project has always built on — a
  transitional fork of `aas-middleware` carrying four synchronization fixes — published under
  its own name. **It is retired within a few releases; do not build on it directly.**

### Known limitations

- Only `resource` and `watchdog` modes are implemented. `server` is reserved and raises
  `NotImplementedError`.
- A `ClassScope` selects *which* parameters, never *which parts* of one. Any chain element
  below a complex property is discarded during fetch, with nothing raised.
- Several consumption rules fail silently rather than raising. They are listed in `AGENTS.md`
  under "These fail silently", and reading that section is not optional if you are writing code
  against this library.
