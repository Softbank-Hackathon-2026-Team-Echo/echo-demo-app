import os

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """REQUIRE_DB_TESTS=1(CI)이면 건너뛴 시험이 하나라도 있을 때 실패로 끝낸다."""
    if os.environ.get("REQUIRE_DB_TESTS") != "1" or exitstatus != 0:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter is not None else 0
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        if reporter is not None:
            reporter.write_line(f"REQUIRE_DB_TESTS=1: 건너뛴 시험 {skipped}건 → 실패", red=True)
