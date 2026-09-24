"""Real model-swap mechanics, safely: a separate test port (8097) and the CPU (no VRAM needed).

Both profiles use the small Bonsai 8B file with different names, so we can see which one is
loaded without needing Bonsai 2's VRAM. Checks:
  - start small, swap to "big", swap back: a new server process each time, the right model name
  - a big model that can't load falls back to the small one, with an event
  - Jarvis's real model server (port 8081) is never touched
  - nothing is left running on the test port afterwards

Run: .venv\\Scripts\\python.exe tests\\qa_models.py      exit code = number of failures
"""
import os
import sys
import time
from pathlib import Path

import psutil

# this suite drives a manager on its OWN test port (8097, CPU), so it opts back in to model control
os.environ.pop("JARVIS_NO_MODEL_CONTROL", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from wk import config  # noqa: E402
from wk.models import ModelManager  # noqa: E402

TMP = ROOT / "qa" / "tmp_models"
TMP.mkdir(parents=True, exist_ok=True)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def real_jarvis_server():
    return ModelManager(dict(config.DEFAULTS), "unused", lambda e: None).our_server_pids()


small = config.DEFAULTS["llm_model_file"]
cfg = dict(config.DEFAULTS, llm_base_url="http://127.0.0.1:8097/v1", llm_gpu_layers=0, llm_ctx=4096,
           away_model_file=small, away_model_alias="qa-big", llm_model="qa-small", away_model_ctx=4096,
           away_model_vram_mb=0, llm_autostart_server=True)
events = []
m = ModelManager(cfg, TMP / "server.log", events.append)
before_real = real_jarvis_server()

try:
    m.startup()
    check("starts the small model", m.loaded_alias() == "qa-small" and m.active == "small", m.loaded_alias())
    first = m.our_server_pids()
    check("exactly one server on the test port", len(first) == 1, str(first))

    t0 = time.time()
    m.switch("big")
    check("swaps up", m.active == "big" and m.loaded_alias() == "qa-big", f"{m.loaded_alias()} in {time.time() - t0:.0f}s")
    second = m.our_server_pids()
    check("the old server was replaced", len(second) == 1 and second != first, f"{first} -> {second}")
    check("describes away mode", m.describe() == "Bonsai 2 27B (away mode)", m.describe())

    m.switch("small")
    check("swaps back", m.active == "small" and m.loaded_alias() == "qa-small", m.loaded_alias())

    m.cfg = dict(cfg, away_model_file=str(TMP / "missing-model.gguf"))
    events.clear()
    m.switch("big")
    check("a big model that can't load falls back to small", m.active == "small" and m.loaded_alias() == "qa-small",
          m.loaded_alias())
    check("...and says so", any("Couldn't load" in e for e in events), str(events))

    check("Jarvis's real server on 8081 untouched", real_jarvis_server() == before_real, f"{before_real} -> {real_jarvis_server()}")
finally:
    m._stop_ours()
    time.sleep(0.5)
check("nothing left on the test port", not m.our_server_pids())
check("no stray llama-server started by the test", all("8097" not in " ".join(p.info["cmdline"] or [])
      for p in psutil.process_iter(["cmdline"]) if (p.info["cmdline"] or [""])[0].lower().endswith("llama-server.exe")))

failed = results.count(False)
print(f"\nmodels: {len(results) - failed} passed, {failed} failed", flush=True)
sys.exit(min(failed, 250))
