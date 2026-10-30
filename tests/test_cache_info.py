#!/usr/bin/env python3
"""cache_info: cache countdown and the cost of a miss, from a result event.

Run: python3 tests/test_cache_info.py
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts"))
import cache_info

fail = []
ev = {"usage": {"iterations": [{"input_tokens": 2, "cache_read_input_tokens": 335006,
                                "cache_creation_input_tokens": 338}],
                "cache_creation": {"ephemeral_1h_input_tokens": 338, "ephemeral_5m_input_tokens": 0}},
      "modelUsage": {"claude-opus-5-5": {}}}
st = cache_info.from_result(ev)
if not (st and st["ctx"] == 335346 and st["ttl"] == 3600 and st["model"] == "claude-opus-5-5"):
    fail.append(f"from_result: {st}")
miss, hit = cache_info.costs(st)
# 335,346 tokens: 1h write at 2 * $4/M = $2.68, cached read at $0.20/M = $0.067
if not (abs(miss - 2.683) < 0.01 and abs(hit - 0.067) < 0.001):
    fail.append(f"costs: {miss}, {hit}")
d = tempfile.mkdtemp()
st["at"] = 1000
open(os.path.join(d, ".cache_state"), "w").write(json.dumps(st))
l = cache_info.line(d, now=1000 + 18 * 60)
if l != "🧊 cache 42m left · miss ≈ $2.68 (cached $0.067)":
    fail.append(f"line: {l!r}")
l = cache_info.line(d, now=1000 + 3700)
if l != "🧊 cache expired · next msg ≈ $2.68":
    fail.append(f"expired line: {l!r}")
if cache_info.line(tempfile.mkdtemp()) != "":
    fail.append("no state should give no line")
if fail:
    print("FAILED:\n  " + "\n  ".join(fail)); sys.exit(1)
print("ok: countdown, miss cost and expiry\nall passed")
