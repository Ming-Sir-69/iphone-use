# Runner-native validation — 2026-10-29

macOS 27.0.1, Xcode 27.1, iPhone 17 Pro / iOS 27.0.1, iPhone Use 0.27.6.
Based on upstream `a1355ab`. Private identifiers and source documents are omitted.

## Current implementation

- The existing signed `.xctrunner` was backed up, staged with the two public
  file-sharing plist keys, and re-signed with the same identity/entitlements.
  `codesign --verify --deep --strict` passed; icon and nested executable preserved.
- Installed into the same runner identity and resumed through the official
  supervisor. `drivable=true`, `runner_actionable=true`, `transport=usb`.
- iOS Files displayed **iPhoneUse-Runner** with its existing icon and the inbox
  folder. The folder was added to Favorites using native UI.
- QQ Mail real PDF → Save to Files → runner inbox → Mac: 109,440 bytes, expected
  SHA-256 matched. Repeating the receive returned `already_identical`.
- Synthetic 16 MiB file seeded into the same runner container → Mac: 16,777,216
  bytes, expected SHA-256 matched. This checks the payload path; the real PDF
  separately covers native Save UI. Two copies were read for stability.
- No Copy action, pasteboard API, shared-pasteboard cache or Handoff-setting
  change was used. Hashes are computed on the Mac, not inside the runner.

`python3 -m unittest discover -s tests -v`: **22 tests passed**. Coverage includes
actual CLI errors/status, cancellation/locks/stale state, lease/UDID/wireless
checks, deadlines, path/metadata/size validation, two-read disagreement, source
changes, expected digest, idempotency, symlinks, non-overwriting publication
races, post-commit receipt errors and preservation of plist identity/icon.
Device errors are mocked for the failure-path tests.

Synthetic bytes: `0..255` repeated 65,536 times (16 MiB).
SHA-256: `341aacac661ccb210720bedaa9ead5d668fe5ea41a73532fc147c71e34040df1`.

## Scope changes and remaining checks

The standalone Swift app and its UI have been removed from the proposal.
Its earlier 23 tests and Debug/Release builds are historical evidence for that
previous prototype, not claimed as Runner-native coverage. This version does
not change Runner source or require a new Swift binary build.

Physical cable removal during a copy, transient route changes between guard
checkpoints, the 64 MiB hardware boundary, multiple devices, older iOS versions,
and persistence through an official rebuild/upgrade remain untested. Native
Save must finish before reception; two stable snapshots alone cannot establish
completion of an unrelated asynchronous writer. This remains a draft experiment
pending setup/signing/cache integration review.
