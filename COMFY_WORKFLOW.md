# Local Comfy image workflow

Jarvis can submit a bounded 384×384 text-to-image job to an already running
ComfyUI service on loopback. The host builds the seven-node graph; the model
can supply only an owner-listed workflow ID and a prompt of at most 400
characters. A job receipt is written before submission. `comfy_submit`
returns a job ID, and `comfy_wait` saves and hashes the PNG only after Comfy
history reports success. A submitted or pending job is not a finished image.

The feature is off until the owner enters a checkpoint filename in Settings
and enables the local Comfy checkbox. The same settings can be added to
`data/config.json`, for example:

```json
{
  "comfy_generation_enabled": true,
  "comfyui_url": "http://127.0.0.1:8188",
  "comfy_workflows": {
    "basic_sd15": {"checkpoint": "dreamshaper_8.safetensors"}
  }
}
```

Merge these keys into the existing owner config; do not replace the whole
file. Jarvis checks that Comfy reports the checkpoint installed and that its
queue is empty. It then requires 13 GPU samples over 60 seconds with at least
4 GB free and at most 10% utilization, and checks the queue again before
submitting. It does not launch Comfy, unload a model,
install assets, or change another job. The output and job receipts stay under
Jarvis's local `data/comfy-artifacts` and `data/comfy-jobs` directories, or the
isolated `JARVIS_DATA_DIR` during QA. Those directories are private runtime
state and are excluded from public source and private non-model backup.

If submission loses its response, the durable ticket is marked uncertain and
Jarvis must reconcile it before another submit. A timed-out wait reports
`not_finished_or_history_unavailable`; it does not claim an image. This first
workflow does not yet provide cancellation, shared GPU leasing, automatic
Comfy startup, image inspection, or restart recovery for a submitted job.
Those remain acceptance work.
