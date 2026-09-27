"""Offline tests for the bounded Comfy image workflow and durable job receipt."""
import json
import struct
import tempfile
import threading
import unittest
import uuid
import zlib
from pathlib import Path
from unittest import mock

from wk import comfy_tools, tool_registry


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + \
            struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 384, 384, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x00\x80\xc0" * 384 for _ in range(384))
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class ComfyToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_patch = mock.patch.object(comfy_tools.config, "DATA_DIR", Path(self.temp.name))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.guard_patch = mock.patch.object(comfy_tools, "_gpu_guard", return_value={
            "samples": 13, "minimum_free_mb": 10000, "maximum_util_percent": 2,
            "sample_interval_seconds": 5})
        self.guard_patch.start()
        self.addCleanup(self.guard_patch.stop)
        self.cfg = {"comfy_generation_enabled": True,
                    "comfyui_url": "http://127.0.0.1:8188",
                    "comfy_workflows": {"basic_sd15": {"checkpoint": "dreamshaper_8.safetensors"}}}

    def test_disabled_or_untrusted_endpoint_has_no_submission(self):
        self.assertFalse(comfy_tools.available({}))
        self.assertNotIn("comfy_submit", tool_registry.selected("generate image", {}))
        with self.assertRaisesRegex(ValueError, "loopback"):
            comfy_tools._base({"comfyui_url": "http://example.com:8188"})
        with self.assertRaisesRegex(ValueError, "workflow"):
            comfy_tools.submit(self.cfg, "unknown", "a cyan orb")

    def test_busy_queue_does_not_submit(self):
        with mock.patch.object(comfy_tools, "_request", side_effect=[
                {"CheckpointLoaderSimple": {"input": {"required": {
                    "ckpt_name": [["dreamshaper_8.safetensors"]]}}}},
                {"queue_running": [[1]], "queue_pending": []}]) as request:
            with self.assertRaisesRegex(RuntimeError, "another job"):
                comfy_tools.submit(self.cfg, "basic_sd15", "a cyan orb")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(list((Path(self.temp.name) / "comfy-jobs").glob("*.json")), [])

    def test_registry_offers_only_configured_image_tools_and_labels_pending(self):
        offered = tool_registry.selected("Please generate image of an orb", self.cfg)
        self.assertIn("comfy_submit", offered)
        self.assertIn("comfy_wait", offered)
        self.assertNotIn("comfy_submit", tool_registry.selected("read a file", self.cfg))
        with mock.patch.object(comfy_tools, "wait", return_value={
                "state": "not_finished_or_history_unavailable", "job_id": str(uuid.uuid4())}) as waiting:
            result = tool_registry.execute("comfy_wait", {"job_id": str(uuid.uuid4())},
                                           offered, timeout_seconds=7)
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(waiting.call_args.args[2], 7)

    def test_cancelled_wait_preserves_submitted_job(self):
        job_id = str(uuid.uuid4())
        prompt_id = str(uuid.uuid4())
        comfy_tools._write_ticket(job_id, {"job_id": job_id, "state": "submitted",
                                          "prompt_id": prompt_id,
                                          "comfy_url": "http://127.0.0.1:8188"})
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(comfy_tools, "_request") as request:
            state = comfy_tools.wait(self.cfg, job_id, 30, cancel)
        self.assertEqual(state["state"], "wait_cancelled_job_not_cancelled")
        request.assert_not_called()
        self.assertEqual(comfy_tools._read_ticket(job_id)["state"], "submitted")

    def test_gpu_guard_fails_closed_before_submission(self):
        self.guard_patch.stop()
        with mock.patch.object(comfy_tools.sensors, "system_stats", return_value={
                "vram_total": 16376, "vram_used": 15000, "gpu": 2}):
            with self.assertRaisesRegex(RuntimeError, "preflight deadline"):
                comfy_tools._gpu_guard(max_seconds=0)

    def test_gpu_guard_waits_out_transient_model_activity(self):
        self.guard_patch.stop()
        busy = {"vram_total": 16376, "vram_used": 5225, "gpu": 70}
        quiet = {"vram_total": 16376, "vram_used": 5225, "gpu": 2}
        with mock.patch.object(comfy_tools.sensors, "system_stats",
                               side_effect=[busy] + [quiet] * 13) as samples, \
                mock.patch.object(comfy_tools.time, "sleep"):
            result = comfy_tools._gpu_guard(max_seconds=100)
        self.assertEqual(samples.call_count, 14)
        self.assertEqual(result["samples"], 13)
        self.assertEqual(result["minimum_free_mb"], 11151)

    def test_submit_wait_and_verify_host_owned_graph(self):
        prompt_id = str(uuid.uuid4())
        png_bytes = _png()
        calls = []

        def request(cfg, route, payload=None, image=False):
            calls.append((route, payload))
            if route.startswith("/object_info/"):
                return {"CheckpointLoaderSimple": {"input": {"required": {
                    "ckpt_name": [["dreamshaper_8.safetensors"]]}}}}
            if route == "/queue":
                return {"queue_running": [], "queue_pending": []}
            if route == "/prompt":
                return {"prompt_id": prompt_id}
            if route == "/history/" + prompt_id:
                return {prompt_id: {"status": {"status_str": "success"},
                                    "outputs": {"10": {"images": [{"filename": "Jarvis_test.png",
                                                                   "type": "output", "subfolder": ""}]}}}}
            if route.startswith("/view?"):
                return png_bytes
            raise AssertionError(route)

        with mock.patch.object(comfy_tools, "_request", side_effect=request):
            receipt = comfy_tools.submit(self.cfg, "basic_sd15", "a cyan orb")
            self.assertEqual(receipt["state"], "submitted")
            graph = calls[3][1]["prompt"]
            self.assertEqual(set(graph), {"4", "5", "6", "7", "8", "9", "10"})
            self.assertEqual(graph["4"]["inputs"]["ckpt_name"], "dreamshaper_8.safetensors")
            self.assertEqual(graph["5"]["inputs"]["width"], 384)
            self.assertEqual(graph["6"]["inputs"]["text"], "a cyan orb")
            finished = comfy_tools.wait(self.cfg, receipt["job_id"], 0)
        self.assertEqual(finished["state"], "complete")
        self.assertEqual(Path(finished["image_path"]).read_bytes(), png_bytes)
        self.assertEqual(comfy_tools.wait(self.cfg, receipt["job_id"], 0)["sha256"], finished["sha256"])
        ticket = json.loads((Path(self.temp.name) / "comfy-jobs" /
                             (receipt["job_id"] + ".json")).read_text())
        self.assertEqual(ticket["state"], "complete")


if __name__ == "__main__":
    unittest.main()
