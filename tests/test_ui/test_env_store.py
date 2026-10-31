from src.ui.env_store import load_env, save_env


def test_save_env_round_trips_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 주석\nA=1\nB=2\n", encoding="utf-8")

    save_env(path, {"B": "3", "C": "4"})

    assert load_env(path) == {"A": "1", "B": "3", "C": "4"}
    assert "# 주석" in path.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_save_env_creates_a_missing_file(tmp_path):
    path = tmp_path / ".env"

    save_env(path, {"A": "1"})

    assert load_env(path) == {"A": "1"}
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
