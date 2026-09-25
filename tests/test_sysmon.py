# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt

"""Tests for the sys.monitoring core in coverage/sysmon.py."""

from __future__ import annotations

import os.path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from coverage import env
from coverage.sysmon import SysMonitor, compute_multiline_map
from tests.coveragetest import CoverageTest
from tests.helpers import holding_sysmon_tool_ids

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
class SysmonToolIdConflictTest(CoverageTest):
    """Tests of SysMonitor's tolerance of sys.monitoring tool id conflicts."""

    def setUp(self) -> None:
        super().setUp()
        self.warnings: list[str] = []

    def capture_warn(self, msg: str, slug: str | None = None, once: bool = False) -> None:
        """A replacement warn() to capture warnings from the tracer."""
        if slug:
            msg = f"{msg} ({slug})"
        self.warnings.append(msg)

    def should_trace(self, filename: str, frame: Any) -> Any:
        """A should_trace() that only traces code in this test file."""
        trace = os.path.abspath(filename) == os.path.abspath(__file__)
        return SimpleNamespace(
            trace=trace,
            source_filename=(filename if trace else None),
        )

    def lock_noop(self) -> None:
        """A no-op to use as lock_data/unlock_data."""

    def make_tracer(self) -> SysMonitor:
        """Make a SysMonitor ready to start."""
        tracer = SysMonitor()
        tracer.data = {}
        tracer.trace_arcs = False
        tracer.should_trace = self.should_trace
        tracer.should_trace_cache = {}
        tracer.lock_data = self.lock_noop
        tracer.unlock_data = self.lock_noop
        tracer.warn = self.capture_warn
        return tracer

    def test_no_tool_id_available_is_tolerated(self) -> None:
        # If all of the sys.monitoring tool ids are taken by other tools,
        # starting the tracer warns instead of raising an error.
        tracer = self.make_tracer()
        with holding_sysmon_tool_ids(6) as held:
            assert len(held) >= 5  # metacov might be holding one id already
            tracer.start()
            assert not tracer.sysmon_on
            tracer.stop()
        assert self.warnings == [
            "Can't use sys.monitoring: no tool id is available, "
            "no data will be collected (sysmon-no-tool-id)",
        ]

    def test_some_tool_ids_taken_still_collects(self) -> None:
        # If another tool is using some of the tool ids, we use one of the
        # remaining ids, and collect data as usual.
        with holding_sysmon_tool_ids(2) as held:
            tracer = self.make_tracer()
            tracer.start()
            try:
                assert tracer.sysmon_on
                assert tracer.myid not in held

                def f() -> int:
                    x = 1
                    x += 2
                    return x

                f()
            finally:
                tracer.stop()
        assert self.warnings == []
        first_line = f.__code__.co_firstlineno
        assert tracer.data[f.__code__.co_filename] == {
            first_line + 1,
            first_line + 2,
            first_line + 3,
        }
