# Changelog

Notable changes to `kapps-semantic-middleware`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

`prepare_release.py` turns the Unreleased heading below into the dated entry for the version
being released, and stops the release if there is no such heading. Write the entry as the work
lands, not at release time.

(That sentence deliberately does not spell the heading out. `edit_changelog` insists on finding
exactly one of it, and prose naming it is a second occurrence — which stopped a release the
first time this file was written.)

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
