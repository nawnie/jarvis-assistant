# Jarvis Assistant release gates

**Last reviewed:** 2026-09-27. This is source-available software under the repository's noncommercial license. The current implementation has meaningful local tests, but a clean installation and all user-visible workflows have not been accepted.

## Evidence at this checkpoint

| Capability | What was observed | Limit |
|---|---|---|
| Windows source and offline checks | The installed desktop and phone source matched the reviewed 111/170-file source manifest. Isolated desktop tests passed 314 with one absent-private-fixture skip; installed desktop tests passed 315. | These are source/test results, not native-screen or physical-phone acceptance. |
| Local Bonsai chat and tools | The configured 8B and 27B profiles loaded under guarded, owner-controlled trials and issued native tool calls. Both read a selected fictional file, used a local allowlisted MCP fixture, and selected a phone-folder tool against a fictional ADB adapter. | The fake adapter never read a physical device. Neither profile has been shown to complete arbitrary autonomous project work. |
| File and project tools | Synthetic tests exercised selected read/write roots, protected-path denial for reads and writes, exact-span patching, registered project tests, and lost-response project lookup. Missing private policy fails closed. | Ordinary Windows process permissions still apply. The registered test runner is not a filesystem sandbox. The current local policy writes only within a dedicated Jarvis workspace. |
| MCP | Both Bonsai profiles selected discovery and an exact call through a one-tool local stdio fixture. The pinned client checked tool schemas and allowlists. | The fixture used fictional data; external services need separate configuration and acceptance. |
| Vision | 8B plus a compact local eyes service read a synthetic `A7` image. | Direct 27B vision answered `AF` twice for that exact image. General image understanding has not been benchmarked. |
| ComfyUI | Isolated ComfyUI 0.37.0 generated neutral 384×384 PNGs with an installed SD1.5 checkpoint. The product adapter completed a direct host call and a Bonsai 8B selected list/submit/wait sequence after 13 quiet GPU samples. The image and exact SHA-256 were inspected; owned services stopped. | The owner-saved live model remains offline and Comfy generation is disabled until configured. Native UI use, cancellation, restart recovery, general workflows, and shared GPU leasing remain open. |
| Phone operator console | The companion's signed synthetic emulator fixture displayed a fictional assignment and a desktop project receipt; live source and offline Android build checks passed. | The physical phone, real PC task completion, and third-party AI task acceptance were not demonstrated. |
| Visual media | Qt-rendered synthetic Now, Chat, Memory, and Settings views were inspected. | Native Windows screen capture and accessibility interaction checks remain open. A synthetic page tour is not a model/task demo. |

## Before a broader release

1. Replace or explicitly review original-machine path defaults and protected-project labels before publishing a portable source projection. Keep the host-side privacy denials and owner-controlled test registry effective.
2. Walk through installation, configuration, app launch, endpoint loss/recovery, memory review, and a bounded project task on a fresh Windows profile. Preserve exact receipts and test data.
3. Resolve direct 27B vision on a fixed image fixture or document it as unsupported. Extend the tested Comfy workflow with owner-facing configuration, cancellation, restart recovery, and shared GPU coordination before claiming general image-generation acceptance.
4. Validate phone pairing and a bounded action on the intended physical device separately. Keep messages as drafts with final Android send confirmation, and keep risky settings behind owner confirmation.
5. Capture an unobstructed native Windows UI flow and accessibility checks. Publish only synthetic or fully inspected, cropped media with captions that describe exactly what the capture proves.
6. Keep model weights, local histories, credentials, device identifiers, and runtime databases out of this repository. Test backup and recovery from an allowlisted non-model archive.

The repository badges describe technology and workflow status, not accreditation. Commercial use remains subject to the [PolyForm Noncommercial license](LICENSE) and separate permission from the project owner.
