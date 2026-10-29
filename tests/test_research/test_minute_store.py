from src.research.minute_store import MinuteStore


def test_empty_store_has_nothing(tmp_path):
    store = MinuteStore(tmp_path)
    assert store.load("069500") == {}
    assert store.last_day("069500") is None


def test_merge_adds_and_overwrites_by_day(tmp_path):
    store = MinuteStore(tmp_path)
    store.merge("069500", {"20261028": [("0900", 100.0)], "20261029": [("0900", 101.0)]})
    store.merge("069500", {"20261029": [("0900", 102.0), ("0901", 103.0)]})

    bars = store.load("069500")
    assert bars["20261028"] == [("0900", 100.0)]
    assert bars["20261029"] == [("0900", 102.0), ("0901", 103.0)]
    assert store.last_day("069500") == "20261029"


def test_tickers_are_separate_files(tmp_path):
    store = MinuteStore(tmp_path)
    store.merge("069500", {"20261029": [("0900", 1.0)]})
    assert store.load("114800") == {}


def test_broken_file_reads_as_empty(tmp_path):
    (tmp_path / "069500.json").write_text("{broken", encoding="utf-8")
    assert MinuteStore(tmp_path).load("069500") == {}
