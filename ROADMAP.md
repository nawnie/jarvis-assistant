# Jarvis Assistant release gates

**Last reviewed:** 2026-09-27. This is source-available software under the repository's noncommercial license. The current implementation has meaningful local tests, but a clean installation and all user-visible workflows have not been accepted.

## Evidence at this checkpoint

| Capability | What was observed | Limit |
|---|---|---|
| Windows source and offline checks | The installed desktop and phone source matched the reviewed 108/170-file source manifest. Independent frozen desktop tests passed 306 with one absent-private-fixture skip; live desktop tests passed 307. | These are source/test results, not first-run or native-screen acceptance. |
| Local Bonsai chat and tools | The configured 8B and 27B profiles loaded under guarded, owner-controlled trials and issued native tool calls. Both selected a phone-folder tool against a fictional ADB adapter. | The fake adapter never read a physical device. Neither profile has been shown to complete arbitrary autonomous project work. |
| File and project tools | Synthetic tests exercised selected read/write roots, protected-path denial for reads and writes, exact-span patching, registered project tests, and lost-response project lookup. Missing private policy fails closed. | Ordinary Windows process permissions still apply. The registered test runner is not a filesystem sandbox. The current local policy writes only within a dedicated Jarvis workspace. |
| MCP | An allowlisted local stdio server worked with the pinned optional MCP client; discovery, schema checks, repeated calls, and cleanup were tested. | External services and their workflows need separate configuration and acceptance. |
| Vision | 8B plus a compact local eyes service read a synthetic `A7` image. | Direct 27B vision answered `AF` twice for that exact image. General image understanding has not been benchmarked. |
| ComfyUI | A synthetic no-model LoadImage→SaveImage workflow completed against an owner-started local service. | No image-generation model or GPU-fit result was accepted. |
| Phone operator console | The companion's signed synthetic emulator fixture displayed a fictional assignment and a desktop project receipt; live source and offline Android build checks passed. | The physical phone, real PC task completion, and third-party AI task acceptance were not demonstrated. |
| Visual media | Qt-rendered synthetic Now, Chat, Memory, and Settings views were inspected. | Native Windows screen capture and accessibility interaction checks remain open. A synthetic page tour is not a model/task demo. |

## Before a broader release

1. Replace or explicitly review original-machine path defaults and protected-project labels before publishing a portable source projection. Keep the host-side privacy denials and owner-controlled test registry effective.
2. Walk through installation, configuration, app launch, endpoint loss/recovery, memory review, and a bounded project task on a fresh Windows profile. Preserve exact receipts and test data.
3. Resolve direct 27B vision on a fixed image fixture or document it as unsupported. Test a safe Comfy image-generation workflow only when GPU ownership and model fit are established.
4. Validate phone pairing and a bounded action on the intended physical device separately. Keep messages as drafts with final Android send confirmation, and keep risky settings behind owner confirmation.
5. Capture an unobstructed native Windows UI flow and accessibility checks. Publish only synthetic or fully inspected, cropped media with captions that describe exactly what the capture proves.
6. Keep model weights, local histories, credentials, device identifiers, and runtime databases out of this repository. Test backup and recovery from an allowlisted non-model archive.

The repository badges describe technology and workflow status, not accreditation. Commercial use remains subject to the [PolyForm Noncommercial license](LICENSE) and separate permission from the project owner.
