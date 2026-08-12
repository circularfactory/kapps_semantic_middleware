"""Offline tests for the examples CLI (#115): copy-out, collision safety, README content."""

from __future__ import annotations

from pathlib import Path

from kapps_semantic_middleware import examples_cli

TOTAL_EXPECTED_FILES = len(examples_cli.PAYLOAD_FILES) + 1  # the payload plus README.md
"""Derived, where it used to be the literal 11.

The count is not what these tests are about -- the payload's *contents* are, and the loop below
asserts every declared name arrives. A hardcoded total only ever fails as arithmetic: #167 added
two compose files to `PAYLOAD_FILES` and this line said 13 != 11, naming neither the feature nor
the fault.
"""


class TestCopyExamples:
    """Tests for copy_examples: file set, exclusions, collision handling."""

    def test_copy_writes_exactly_payload_plus_readme(self, tmp_path: Path) -> None:
        """copy_examples writes the 10 payload files plus README.md."""
        written = examples_cli.copy_examples(tmp_path)

        assert len(written) == TOTAL_EXPECTED_FILES
        assert (tmp_path / "README.md").exists()
        for name in examples_cli.PAYLOAD_FILES:
            assert (tmp_path / name).exists(), f"{name} should have been copied"

    def test_the_compose_files_arrive_together_in_a_docker_subdirectory(
        self, tmp_path: Path
    ) -> None:
        """#157: a pip-installing user must be able to run the documented `cd docker` line.

        Both files, in one directory, and that directory created by the copy rather than
        assumed. `docker-compose.yml` mounts `./graphdb-repo-config.ttl` as a **sibling path**,
        so a copy that flattened them or dropped either one would produce a compose file that
        starts GraphDB and then fails to create the repository -- with a container error rather
        than anything naming the missing file.
        """
        examples_cli.copy_examples(tmp_path)

        compose = tmp_path / "docker" / "docker-compose.yml"
        config = tmp_path / "docker" / "graphdb-repo-config.ttl"
        assert compose.is_file(), "the compose file did not reach the copied directory"
        assert config.is_file(), "the repository config did not reach the copied directory"
        assert "graphdb-repo-config.ttl" in compose.read_text(encoding="utf-8"), (
            "the compose file no longer mounts the config beside it; the two may have been "
            "split, which this test exists to prevent"
        )

    def test_copy_does_not_create_init_or_context_or_docs(self, tmp_path: Path) -> None:
        """copy_examples does not copy __init__.py, CONTEXT.md, or docs/."""
        examples_cli.copy_examples(tmp_path)

        assert not (tmp_path / "__init__.py").exists()
        assert not (tmp_path / "CONTEXT.md").exists()
        assert not (tmp_path / "docs").exists()

    def test_collision_without_force_leaves_existing_file_untouched(self, tmp_path: Path) -> None:
        """With force=False (default), an existing file is skipped and left unchanged."""
        sentinel = b"# THIS IS MY SENTINEL CONTENT\n"
        seed_path = tmp_path / "seed.py"
        seed_path.write_bytes(sentinel)

        written = examples_cli.copy_examples(tmp_path)

        assert seed_path not in written
        assert seed_path.read_bytes() == sentinel

    def test_collision_with_force_overwrites_existing_file(self, tmp_path: Path) -> None:
        """With force=True, an existing file is overwritten with the bundled content."""
        sentinel = b"# THIS IS MY SENTINEL CONTENT\n"
        seed_path = tmp_path / "seed.py"
        seed_path.write_bytes(sentinel)

        written = examples_cli.copy_examples(tmp_path, force=True)

        assert seed_path in written
        content = seed_path.read_bytes()
        assert content != sentinel
        assert content.startswith(b'"""Self-contained seeding')

    def test_readme_contains_key_run_strings(self, tmp_path: Path) -> None:
        """The generated README.md carries the essential run instructions."""
        examples_cli.copy_examples(tmp_path)
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")

        assert "kapps-transferunit-factory" in readme
        assert "scenario1_hello_world.py" in readme
        assert "Jupyter" in readme or "notebook" in readme.lower()


class TestMain:
    """Tests for the main() argparse entry point."""

    def test_main_returns_zero_and_populates_directory(self, tmp_path: Path) -> None:
        """main() returns 0 and writes files to the given destination."""
        dest = tmp_path / "my_examples"
        result = examples_cli.main([str(dest)])

        assert result == 0
        assert (dest / "README.md").exists()
        assert (dest / "scenario1_hello_world.py").exists()

    def test_main_with_force_flag(self, tmp_path: Path) -> None:
        """main() honors --force."""
        dest = tmp_path / "forced_examples"
        dest.mkdir()
        (dest / "seed.py").write_bytes(b"# sentinel\n")

        result = examples_cli.main([str(dest), "--force"])

        assert result == 0
        content = (dest / "seed.py").read_bytes()
        assert content != b"# sentinel\n"
        assert content.startswith(b'"""Self-contained seeding')
