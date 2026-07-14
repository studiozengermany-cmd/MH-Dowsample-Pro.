from library_layout import friendly_filename, library_parts


def test_content_first_library_paths() -> None:
    assert library_parts({"content_type": "loop", "genre_hint": "deep-house"}) == (
        "Loops",
        "Deep House",
    )
    assert library_parts({"content_type": "one-shot", "genre_hint": "one-shot"}) == ("One-Shots",)
    assert library_parts({"content_type": "fx", "genre_hint": "fx"}) == ("FX",)
    assert library_parts({"content_type": "loop", "genre": "hip-hop"}) == (
        "Loops",
        "Hip Hop",
    )


def test_opaque_cdn_name_becomes_readable() -> None:
    name = friendly_filename(
        "98baa94c88dc94b8666ab2eb5b4e3e2a.mp3",
        {
            "content_type": "loop",
            "genre_hint": "deep-house",
            "bpm": 120,
            "bpm_confidence": "high",
            "key": "Fmin",
        },
        "98baa94c88dc94b8666ab2eb5b4e3e2a",
    )
    assert name == "Deep House Loop - 120 BPM - F minor - 98baa94c.wav"


def test_readable_source_name_is_preserved() -> None:
    name = friendly_filename(
        "warm_vinyl_kick.wav",
        {"content_type": "one-shot", "key": "C#min"},
        "abcdef123456",
    )
    assert name == "warm vinyl kick - C sharp minor.wav"
