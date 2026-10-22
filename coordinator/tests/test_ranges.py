import itertools

import pytest

from app.ranges import (
    MAX_ID,
    IdRange,
    RangeError,
    chunk_count,
    from_to,
    next_chunk,
    parse_range,
    split_range,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[0,500000]", IdRange(0, 500000)),
        ("[5,5]", IdRange(5, 5)),
        ("[ 10 , 20 ]", IdRange(10, 20)),
        ("100", IdRange(0, 100)),
        ("0", IdRange(0, 0)),
        (" 42 ", IdRange(0, 42)),
        ("[7,]", IdRange(7, None)),
        ("[ 7 , ]", IdRange(7, None)),
        ("", IdRange(0, None)),
        ("   ", IdRange(0, None)),
        (None, IdRange(0, None)),
    ],
)
def test_parse_valid(text, expected):
    assert parse_range(text) == expected


@pytest.mark.parametrize(
    "text",
    ["[5,3]", "-1", "[-1,5]", "[1,-5]", "abc", "[a,b]", "[,5]", "[1;5]", "1,5", "[1,5", "1.5", "1e3", "٣", "[1,2,3]"],
)
def test_parse_invalid(text):
    with pytest.raises(RangeError):
        parse_range(text)


def test_too_large_id_rejected():
    with pytest.raises(RangeError):
        parse_range(str(MAX_ID + 1))


def test_str_roundtrip():
    for text in ("[0,10]", "[3,]", "[0,]"):
        assert str(parse_range(text)) == text
    assert parse_range(str(parse_range("[4,9]"))) == IdRange(4, 9)


def test_from_to():
    assert from_to(None, None) == IdRange(0, None)
    assert from_to(5, None) == IdRange(5, None)
    assert from_to(None, 9) == IdRange(0, 9)
    assert from_to(3, 8) == IdRange(3, 8)
    with pytest.raises(RangeError):
        from_to(9, 3)


def test_size_contains_infinite():
    r = IdRange(10, 19)
    assert r.size() == 10 and not r.is_infinite
    assert r.contains(10) and r.contains(19) and not r.contains(9) and not r.contains(20)
    inf = IdRange(10, None)
    assert inf.size() is None and inf.is_infinite
    assert inf.contains(10**12) and not inf.contains(9)


@pytest.mark.parametrize(
    "start, end, size",
    [(0, 0, 1), (0, 9, 3), (0, 9, 10), (0, 9, 100), (5, 1004, 1000), (3, 3, 5), (0, 499999, 7000)],
)
def test_chunks_cover_range_without_gaps_or_overlaps(start, end, size):
    chunks = list(split_range(IdRange(start, end), size))
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (a1, b1), (a2, _) in itertools.pairwise(chunks):
        assert a2 == b1 + 1  # ни пропусков, ни повторов
    assert sum(b - a + 1 for a, b in chunks) == end - start + 1
    assert all(b - a + 1 <= size for a, b in chunks)
    assert len(chunks) == chunk_count(IdRange(start, end), size)


def test_last_chunk_is_trimmed():
    assert list(split_range(IdRange(0, 24), 10)) == [(0, 9), (10, 19), (20, 24)]


def test_infinite_range_yields_endlessly():
    first = list(itertools.islice(split_range(IdRange(5, None), 100), 4))
    assert first == [(5, 104), (105, 204), (205, 304), (305, 404)]
    assert chunk_count(IdRange(5, None), 100) is None


def test_resume_after_last_end():
    rng = IdRange(0, 99)
    assert list(split_range(rng, 30, last_end=59)) == [(60, 89), (90, 99)]
    assert next_chunk(rng, 30, 99) is None


def test_next_chunk_validation():
    with pytest.raises(RangeError):
        next_chunk(IdRange(0, 10), 0, None)
    with pytest.raises(RangeError):
        next_chunk(IdRange(100, 200), 10, 5)


def test_chunk_never_exceeds_max_id():
    last = MAX_ID - 3
    assert next_chunk(IdRange(0, None), 100, last - 1) == (last, MAX_ID)
    assert next_chunk(IdRange(0, None), 100, MAX_ID) is None
