"""Offline test: OpenMeteoClient retries a 200 whose JSON body was cut off.

The deploy on 12 Oct 2026 died on `JSONDecodeError: Expecting ',' delimiter`
after Open-Meteo dropped a connection mid-response.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_connectors import open_meteo_client as om  # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class Resp:
    def __init__(self, text, status=200):
        self.status_code, self.text, self.headers = status, text, {}

    def json(self):
        import json
        try:
            return json.loads(self.text)
        except ValueError as e:  # what requests raises is a ValueError subclass
            raise requests.exceptions.JSONDecodeError(str(e), self.text, 0)


def run(bodies, retries=3):
    calls = []
    def fake_get(*a, **k):
        calls.append(1)
        return bodies[min(len(calls), len(bodies)) - 1]
    real_get, real_sleep = om.requests.get, om.time.sleep
    om.requests.get, om.time.sleep = fake_get, lambda s: None
    try:
        c = om.OpenMeteoClient("https://example.invalid", max_retries=retries)
        return c._request_with_retry({}), len(calls), None
    except Exception as e:  # noqa: BLE001
        return None, len(calls), e
    finally:
        om.requests.get, om.time.sleep = real_get, real_sleep


out, n, err = run([Resp('[{"latitude": 46.5, "hourly": {"time": ["a", "b"'), Resp('[{"latitude": 46.5}]')])
check("truncated body is retried, then the good one is returned", out == [{"latitude": 46.5}] and n == 2, f"calls={n} err={err!r}")

out, n, err = run([Resp('{"cut')], retries=3)
check("always truncated -> gives up after max_retries with a clear error",
      out is None and n == 3 and isinstance(err, RuntimeError), f"calls={n} err={err!r}")

out, n, err = run([Resp('{"ok": 1}')])
check("good body on the first try needs one call", out == {"ok": 1} and n == 1)

if fails:
    print(f"\n{len(fails)} FAILED"); sys.exit(1)
print("\nall passed")
