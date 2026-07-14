from pathlib import Path

import pytest

from exceptions import PathTraversalError
from utils.paths import safe_child, sanitize_component, sanitize_filename


@pytest.mark.parametrize(
    "value", ["../x.wav", "..\\x.wav", "C:\\x.wav", "/tmp/x.wav", "x/y.wav", "x\x00.wav"]
)
def test_rejects_traversal(value: str) -> None:
    with pytest.raises(PathTraversalError):
        sanitize_filename(value)


def test_sanitizes_safe_name() -> None:
    assert sanitize_component("hip:hop") == "hip_hop"


def test_safe_child_stays_contained(tmp_path: Path) -> None:
    assert safe_child(tmp_path, "site", "genre").is_relative_to(tmp_path.resolve())
