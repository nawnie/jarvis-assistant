# Local Comfy image workflow

Jarvis can submit a bounded 384×384 text-to-image job to an already running
ComfyUI service on loopback. The host builds the seven-node graph; the model
can supply only an owner-listed workflow ID and a prompt of at most 400
characters. A job receipt is written before submission. `comfy_submit`
returns a job ID, and `comfy_wait` saves and hashes the PNG only after Comfy
history reports success. A submitted or pending job is not a finished image.
For a user request to cancel a specific Jarvis job, `comfy_cancel` sends its
recorded prompt ID to ComfyUI's targeted job-cancel route. It never uses the
global interrupt or clears someone else's queue. `cancel_requested` means the
request was dispatched; `cancelled` is reported only after Comfy history
records an execution interruption. Older Comfy servers without that route
fail closed.

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

Jarvis records its own prompt UUID in the durable ticket before submission.
If submission loses its response, the ticket is marked uncertain; `comfy_wait`
can query that UUID after reconnection without submitting another image.
Never infer that an absent history entry proves the original request was not
accepted. A timed-out wait reports
`not_finished_or_history_unavailable`; it does not claim an image. This first
workflow does not yet provide shared GPU leasing, automatic Comfy startup,
or image inspection. Targeted cancellation and live restart recovery still
need real-server acceptance; a local fixture only proved that a fresh process
can resume a persisted submitted ticket when history is available. Lost
submission responses with no later queue/history evidence remain uncertain.
Those remain acceptance work.
