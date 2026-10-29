# Validation — 2026-10-29

Environment: macOS 27.0.1, Xcode 27.1 (27A9275), iPhone 17 Pro on iOS
27.0.1, installed iPhone Use 0.27.6. Experiment based on upstream `a1355ab`.
The development app was signed and installed using the tester's own team.
Identifiers, signing material, invoices and local filesystem paths are omitted.

## Automated checks

`python3 -m unittest discover -s tests -v`: **23 tests passed**.

Covered: actual CLI error/status entrypoints, lock contention and stale state,
token cancellation, owner and target mismatch, wireless rejection, bounded
subprocess timeout, malformed/path-traversal/oversized manifests, stale pointer
timeout, immutable manifest mismatch, corrupt payload, expected hash mismatch,
lease loss before commit, existing identical/different targets, symlink targets,
concurrent destination creation, failed publication cleanup, empty and multi-chunk
files, private destination permissions, and post-commit acknowledgement/receipt
failures. Failure-path device operations are mocked and do not prove hardware
behavior by themselves.

`bash -n build-install.sh`: passed.

Debug device build/install: passed. Release generic-iOS build with signing
disabled: passed. The Debug-only `--usb-bridge-fixture` string is absent from
the Release executable.

## Device checks of this experiment

| Check | Result |
| --- | --- |
| QQ Mail real PDF → system share/Open In → USB File Bridge → Mac | Passed; 109,440 bytes; expected and received SHA-256 equal |
| iOS import identity distinct from prior inbox item | Passed; receiver was armed before sharing |
| Synthetic 16 MiB file through Debug fixture hook and the same importer | Passed; 16,777,216 bytes and SHA-256 equal |
| Phone acknowledgement after local verification | Passed for both transfers; UI showed “Received and verified on Mac” |
| CoreDevice connection checkpoints | `wired` before/after receive and before commit |
| Second active receiver | Rejected without consuming a file |
| Cooperative `stop` | Receiver exited with cancellation JSON; status became inactive |
| `phone_release_owner` during an armed wait | Receiver exited; status became inactive; no file committed |
| Apple Universal Clipboard | No Copy action, pasteboard API, shared-pasteboard cache, or Handoff-setting change used |

Synthetic 16 MiB sample: the byte sequence `0..255` repeated 65,536 times.
SHA-256: `341aacac661ccb210720bedaa9ead5d668fe5ea41a73532fc147c71e34040df1`.
It was seeded into this app's own `Documents/Fixtures` container over the cable
and then imported by the Debug hook. That test validates the payload/importer/
receiver path, **not** the iOS share UI; the real PDF check separately covers UI.

## Not established

- Physical cable removal during download and a wired→wireless→wired transition
  between guard checkpoints have not been exercised.
- Wireless transfer is deliberately rejected; no wireless hardware pass is claimed.
- The 64 MiB limit, other devices/iOS releases, multiple concurrent shares,
  multiple phone instances on hardware, and long-term retention are untested.
- This does not establish production readiness or provide a persistent clipboard
  service. A production integration needs lifecycle/queue/cleanup UX and a
  daemon-native ownership/transport contract.
