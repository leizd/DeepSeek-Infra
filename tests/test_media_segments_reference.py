"""The retained corpus must still be produced by the original implementation."""
import json
from pathlib import Path

from scripts.capture_media_segments_reference import build_reference


def test_media_segments_reference_matches_original_processors():
    path=Path(__file__).resolve().parent/"native/media_segments_reference_v1.json"
    expected=json.loads(path.read_bytes())
    observed=build_reference()
    assert observed==expected
    assert len(observed["cases"])==28
    assert sum("expected" in row for row in observed["cases"])==25
    assert sum("error" in row for row in observed["cases"])==3
