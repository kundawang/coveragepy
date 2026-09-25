# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Tests for the sys.monitoring core in coverage/sysmon.py."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from unittest import mock

import pytest

import coverage
from coverage import env
from coverage.sysmon import SysMonitor, compute_multiline_map
from tests.coveragetest import CoverageTest

MULTI_PY = "x = (\n    1 +\n    2\n)\ny = 5\n"
MULTI_MAP = {1: 1, 2: 1, 3: 1, 4: 1}


class ComputeMultilineMapTest(CoverageTest):
    """Tests of compute_multiline_map."""

    def test_python_file(self) -> None:
        self.make_file("multi.py", MULTI_PY)
        assert compute_multiline_map("multi.py") == MULTI_MAP

    def test_missing_file(self) -> None:
        # A code object can name a file that doesn't exist on disk.
        assert compute_multiline_map("no_such_file.py") == {}

    def test_non_python_file(self) -> None:
        # A code object can point at non-Python source, as with compiled
        # template files.  Tokenizing fails, and the map is empty.
        self.make_file(
            "widget.html",
            # The unbalanced paren makes this untokenizable as Python.
            "<div class=(widget>\n    {% if widget.name %}\n</div>\n",
        )
        assert compute_multiline_map("widget.html") == {}

    def test_indentation_error(self) -> None:
        self.make_file("bad.py", "def f():\n        pass\n  huh = 3\n")
        assert compute_multiline_map("bad.py") == {}


@pytest.mark.skipif(not env.PYBEHAVIOR.pep669, reason="SysMonitor needs sys.monitoring")
class MultilineMapCacheTest(CoverageTest):
    """Tests of SysMonitor's per-tracer multiline map cache."""

    def test_each_file_computed_at_most_once(self) -> None:
        self.make_file("multi.py", MULTI_PY)
        self.make_file("other.py", "a = [1,\n    2]\n")
        tracer = SysMonitor()
        with mock.patch(
            "coverage.sysmon.compute_multiline_map",
            side_effect=compute_multiline_map,
        ) as computer:
            map1 = tracer.get_multiline_map("multi.py")
            map2 = tracer.get_multiline_map("multi.py")
            other = tracer.get_multiline_map("other.py")
        assert computer.call_count == 2
        assert map1 is map2
        assert map1 == MULTI_MAP
        assert other == {1: 1, 2: 1}

    def test_cache_dies_with_the_tracer(self) -> None:
        # The cache is per-instance: a new tracer re-reads the file, so a
        # source file changed between runs can't serve a stale map, the way
        # a module-level cache could.
        self.make_file("multi.py", MULTI_PY)
        tracer = SysMonitor()
        assert tracer.get_multiline_map("multi.py") == MULTI_MAP
        self.make_file("multi.py", "x = 1\ny = (2 +\n    3)\n")
        assert tracer.get_multiline_map("multi.py") == MULTI_MAP  # cached
        assert SysMonitor().get_multiline_map("multi.py") == {2: 2, 3: 2}


@pytest.mark.skipif(not env.PYBEHAVIOR.pep669, reason="SysMonitor needs sys.monitoring")
class SysmonConflictTest(CoverageTest):
    """Tests of how the sysmon core tolerates other sys.monitoring tools."""

    def setUp(self) -> None:
        super().setUp()
        self.set_environ("COVERAGE_CORE", "sysmon")

    @contextlib.contextmanager
    def tool_ids_in_use(self, count: int) -> Iterator[None]:
        """Grab `count` sys.monitoring tool ids, as another tool would."""
        grabbed = []
        try:
            for tool_id in range(count):
                # Some ids might already be in use, perhaps by metacov.
                with contextlib.suppress(ValueError):
                    sys.monitoring.use_tool_id(tool_id, f"other-tool-{tool_id}")
                    grabbed.append(tool_id)
            yield
        finally:
            for tool_id in grabbed:
                sys.monitoring.free_tool_id(tool_id)

    def test_all_ids_in_use_falls_back_to_pytrace(self) -> None:
        self.make_file("numbers.py", "print(123, 456)\n")
        cov = coverage.Coverage()
        with self.tool_ids_in_use(6):
            with self.assert_warnings(
                cov,
                [r"Can't use sys.monitoring: all of its tool ids are in use by other tools"],
            ):
                self.start_import_stop(cov, "numbers")
            # The sysmon core couldn't be used, so we fell back to pytrace.
            assert cov._collector.tracer_name() == "PyTracer"
        # Measurement continued despite the conflict.
        report = self.get_report(cov)
        assert "numbers.py 1 0 100%" in report

    def test_some_ids_in_use_still_uses_sysmon(self) -> None:
        self.make_file("numbers.py", "print(123, 456)\n")
        cov = coverage.Coverage()
        with self.tool_ids_in_use(3):
            with self.assert_warnings(cov, []):
                self.start_import_stop(cov, "numbers")
            # There were still tool ids available, so sysmon was used.
            assert cov._collector.tracer_name() == "SysMonitor"
        report = self.get_report(cov)
        assert "numbers.py 1 0 100%" in report

    def test_no_conflict_uses_sysmon(self) -> None:
        self.make_file("numbers.py", "print(123, 456)\n")
        cov = coverage.Coverage()
        with self.assert_warnings(cov, []):
            self.start_import_stop(cov, "numbers")
        assert cov._collector.tracer_name() == "SysMonitor"
        report = self.get_report(cov)
        assert "numbers.py 1 0 100%" in report
