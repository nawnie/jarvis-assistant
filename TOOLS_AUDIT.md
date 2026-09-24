# Tool boundaries

`/tools` checks whether the Claude and Codex CLIs are installed at runtime. Bonsai 8B can request one bounded, model-only consultation when that feature is enabled in Memory Core. The consulted CLI does not receive Jarvis's memory database or the earlier chat transcript, and its installed MCP servers are not passed through.

`/processes` reads a resource snapshot. VRAM values appear only when the GPU driver reports a per-process compute figure. `/stop PID IDENTITY` requires an explicit user command and a process start-time match; a model suggestion cannot invoke it.

The app does not include model weights, an image-generation bridge, or the installed CLI configurations. Qwen Chat's image facade is a separate local service and is not currently wired into Jarvis.
