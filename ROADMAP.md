# Jarvis Assistant MVP Roadmap

**Last reviewed:** 2026-09-24

**Status:** MVP hardening in progress. The Windows app, setup guide, and deterministic checks are present. End-to-end model-backed operation on a clean user installation has not been demonstrated.

## Current checkpoint

- The core suite passed **81 tests** on the local Windows source checkout, and `pyflakes` reported no findings. These checks do not prove live model inference or installation on another PC.
- `/status` now probes the configured local model endpoint and reports a credential-free failure reason. The change has unit coverage; the running Jarvis process has not been restarted to load it yet.
- During the 2026-09-24 review, Jarvis's configured local model endpoint had no server listening. Model-backed chat through the Jarvis app therefore remains unverified.
- A text-only Bonsai 2 27B CLI review ran separately under the local VRAM guard. It reviewed supplied release-readiness notes; it did not run inside Jarvis, change model weights, or add memory to Jarvis.
- GitHub Actions run [36062613907](https://github.com/nawnie/jarvis-assistant/actions/runs/36062613907) passed for this branch's status change on 2026-09-24. It runs the repository's lint and core-test workflow; it does not prove live model inference or a user-profile install.

## Release milestones

| Milestone | Acceptance evidence | State |
|---|---|---|
| Reliable offline behavior | Safe sample config; app starts without monitoring or model autostart; status explains endpoint failures without disclosing credentials | Code and isolated checks; restart/live check pending |
| First-run Windows acceptance | Follow setup from a fresh Windows user profile, configure data and model paths, start and stop cleanly, and verify recovery after endpoint loss and return | Not demonstrated |
| Conversation MVP | Exercise chat, memory suggestions, explicit KEEP review, and one bounded project objective against the intended local model; retain the test scope and receipt | Not demonstrated through Jarvis's model endpoint |
| 27B away-work review | Show the configured model fits available VRAM with guard headroom, produces a useful review, and unloads cleanly; keep any file changes proposal-only until user approval | Planned; no Jarvis-integrated acceptance |
| User release package | Document install, upgrade, backup, recovery, data location, supported Windows/Python versions, and known limitations; validate on a clean machine | Not demonstrated |
| Commercial release decision | Obtain separate project-owner license permission and qualified legal review before commercial use | Open; current license is noncommercial |

## Safe learning boundary

Jarvis can use selected activity and recent local Codex or Claude Code request hints only when the matching settings and Watching are enabled. Observations are bounded context, not proof that a project task completed. Durable memory suggestions require review. Observed tasks do not become independent objectives automatically. The planned 27B self-repair path must remain proposal-only, show its evidence and diff, and require explicit approval before applying changes.

## Badges and accreditation

The README badges describe the GitHub Actions workflow, platform, Python/UI stack, model links, and current license. They are not certification marks. No third-party certification or accreditation evidence is documented in the reviewed repository, and none is claimed here.

## Next work

1. Activate the `/status` change through a controlled Jarvis restart and verify the live response while the model endpoint is unavailable.
2. Recheck endpoint recovery and a real chat turn when the intended model service can be run within the owner's VRAM constraints.
3. Complete the fresh-profile install, backup/recovery, and bounded project-objective walkthrough.
4. Add release packaging and a user-facing upgrade path after the acceptance steps above are repeatable.
5. Keep commercial distribution gated on written license permission and legal review; do not infer permission from repository visibility or CI badges.
