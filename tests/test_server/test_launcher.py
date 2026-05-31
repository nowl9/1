"""Static sanity checks for the one-click dashboard launcher (dashboard.bat).

Spawning the real .bat (a long-running server + a browser) is not practical
headless, so this verifies the launcher's *contract* by inspecting the script:
it runs the right module, waits for readiness, opens the browser, and stays
localhost-only.  The runtime invariant it depends on -- the app serving "/"
and "/api/health" on localhost -- is covered by test_dashboard.py
(test_health_* / test_spa_served_at_root).

Kept network-free (no TestClient) so the launcher checks are deterministic
under pytest-randomly.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BAT = _REPO_ROOT / "dashboard.bat"


def test_bat_exists_and_ascii() -> None:
    assert _BAT.is_file(), "dashboard.bat must live at the repo root"
    raw = _BAT.read_bytes()
    assert raw, "dashboard.bat is empty"
    raw.decode("ascii")  # raises if any non-ASCII byte slipped in


def test_bat_launcher_contract() -> None:
    text = _BAT.read_text(encoding="ascii")
    # Starts the agent (which hosts the dashboard) via the canonical command.
    assert "btc_pm_arb.main" in text
    assert "py -3.12" in text
    # Waits for readiness, then opens the dashboard in the browser.
    assert "/api/health" in text
    assert "Start-Process" in text
    # Default mode is paper; replay is handled (and is headless).
    assert "MODE=paper" in text
    assert "replay" in text


def test_bat_is_localhost_only() -> None:
    text = _BAT.read_text(encoding="ascii")
    assert "127.0.0.1" in text
    # Must not expose anything network-accessible.
    assert "0.0.0.0" not in text
