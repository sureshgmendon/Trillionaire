"""Global pytest fixtures ensuring isolated disk state across tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_daily_state_dir(tmp_path, monkeypatch):
    """Isolates DailyStateStore JSON persistence per test so test runs never pollute logs/ or cross-contaminate."""
    state_dir = tmp_path / "daily_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TRILLIONAIRE_STATE_DIR", str(state_dir))
    yield state_dir
