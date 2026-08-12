"""The middleware mode constants (#21, ADR 0005).

Pure unit tests — no GraphDB, no network. The point of the type is that a typo cannot reach
the constructor's mode check unnoticed, and that every existing caller passing a bare string
keeps working.
"""

from __future__ import annotations

import pytest

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware import modes


class TestConstants:
    def test_the_three_modes_of_adr_0005(self):
        assert {str(m) for m in modes.ALL} == {"resource", "server", "watchdog"}

    def test_a_mode_equals_its_string(self):
        """Mode subclasses str, which is what keeps existing callers working."""
        assert Mode.RESOURCE == "resource"
        assert Mode.WATCHDOG == "watchdog"

    def test_a_mode_renders_as_its_value(self):
        """Without __str__, an f-string would render 'Mode.RESOURCE' and change every log
        line and error message that interpolates a mode."""
        assert f"{Mode.RESOURCE}" == "resource"

    def test_a_bare_string_is_still_a_valid_mode(self):
        """The constant is the better way to say it; the string does not stop being a way."""
        assert "resource" in modes.ALL


class TestValidation:
    def test_a_typo_is_rejected_and_the_message_lists_the_alternatives(self):
        with pytest.raises(ValueError) as exc:
            SemanticMiddleware(mode="resourse")

        message = str(exc.value)
        assert "resourse" in message
        for mode in modes.ALL:
            assert str(mode) in message

    def test_the_constant_and_the_string_take_the_same_path(self):
        """Both should fail on the *same* next check — the missing resource-mode arguments —
        rather than one of them falling through the mode validation differently."""
        with pytest.raises(ValueError) as from_constant:
            SemanticMiddleware(mode=Mode.RESOURCE)
        with pytest.raises(ValueError) as from_string:
            SemanticMiddleware(mode="resource")

        assert str(from_constant.value) == str(from_string.value)
        assert "resource mode requires" in str(from_constant.value)

    def test_server_mode_is_still_unimplemented(self):
        """Reserved, and ruled out of scope for the controller in ADR 0005's amendment."""
        with pytest.raises(NotImplementedError):
            SemanticMiddleware(mode=Mode.SERVER)
