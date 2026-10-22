#!/usr/bin/env python3
"""Dashboard reports measured memory and valid CPU deltas, not placeholder zeros."""
import math
import shutil
import tempfile
import time
from test_feed import Server
from test_feed_versions import catalog


def main():
    work = tempfile.mkdtemp(prefix="rex-resource-metrics-")
    catalog(work)
    server = Server(work, 9)
    try:
        status, body = server.request("/statistics/resources?timespan=6")
        assert status == 200, (status, body)
        first = body["MediaContainer"]["StatisticsResources"][-1]
        # These are consumer-visible utilization percentages on the isolated host.
        assert all(0 < first[name] <= 100 for name in ("processMemoryUtilization", "hostMemoryUtilization")), first
        assert all(math.isfinite(first[name]) for name in ("processMemoryUtilization", "hostMemoryUtilization")), first
        time.sleep(1.1)
        status, body = server.request("/statistics/resources?timespan=6&at%3E=" + str(first["at"]))
        rows = body["MediaContainer"]["StatisticsResources"]
        assert status == 200 and all(row["at"] > first["at"] for row in rows), (status, body)
        assert rows and all(0 <= row[name] <= 100 for row in rows
                            for name in ("hostCpuUtilization", "processCpuUtilization")), rows
        status, body = server.request("/statistics/resources?timespan=5")
        assert status == 200 and body["MediaContainer"]["size"] == 0, (status, body)
        print("PASS measured dashboard memory, CPU deltas and resource query boundaries")
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
