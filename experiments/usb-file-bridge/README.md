# USB file inbox experiment

An explicit iPhone → Mac file handoff when Universal Clipboard does not sync.
The user (or an authorized agent) shares a document into a small signed iOS app;
a one-shot Mac receiver pulls that app's own payload through CoreDevice and
verifies its size and SHA-256 before publishing a local file.

This is an opt-in experiment, **not** a new daemon endpoint, MCP tool, background
clipboard synchronizer, or production release feature. It does not change
Handoff, read either clipboard, or read another app's private container.

## Requirements and build

- macOS with Xcode/CoreDevice, XcodeGen, Python 3.9+, and an installed `iphone-use` CLI.
- A paired, unlocked iPhone, connected by USB; iOS 17+ for this app.
- Your own development signing team and unique bundle identifier. No credentials,
  certificates, device identifiers or provisioning profiles are included here.

From this directory:

```sh
DEVICE_UDID='<your-udid>' TEAM_ID='<your-team>' BUNDLE_ID='<your-bundle-id>' \
  bash build-install.sh
```

Launch **USB File Bridge** once to initialize its versioned inbox. The build
script generates the Xcode project and uses your existing Xcode account; it does
not sign in or resolve trust prompts. Personal-team provisioning expires and
requires rebuilding. Installing with an identifier used by another app replaces
that app, so choose a dedicated identifier.

## Receive one file

1. Use the normal iPhone Use session to select and verify the source document.
   Check `phone_status` for `drivable`, the exact device UDID, and your control
   lease's `owner`. Do not borrow another caller's owner value.
2. Start this command in a background terminal/task and wait for `event=armed`:

   ```sh
   python3 mac/receive.py receive \
     --device '<exact-udid>' --bundle '<your-bundle-id>' \
     --instance default --owner '<your-session-owner>' \
     --destination '/absolute/output/directory' --timeout 180
   ```

   `IPHONE_USE_OWNER` can supply `--owner`. `--state-dir` relocates local runtime
   state; by default it is under `~/.iphone-use/usb-file-bridge`. Instance, UDID
   and bundle form the lock scope. Optional `--expect-sha256` verifies a known
   sample. The default and maximum payload limit is 64 MiB.
3. On the phone, use the document's system share/Open In menu and choose
   **USB File Bridge** (possibly under More). Do not choose Copy. In QQ Mail,
   the tested entry is invoice detail → Share → Open in another app.
4. Wait for `event=received`, `committed=true`. The phone receives a separate
   acknowledgement; `phone_ack=false` means the local file still committed but
   the phone has not confirmed receipt. The UI never treats import alone as
   transfer success.

Commands use the same device/bundle/instance/state-dir arguments:

```sh
python3 mac/receive.py status --device '<udid>' --bundle '<bundle>'
python3 mac/receive.py stop   --device '<udid>' --bundle '<bundle>'
```

`stop` requests cooperative cancellation using the active session token. It
never signals a PID from a stale file. `status.active` uses an OS lock rather
than PID existence. Agents should select **one** route per task: while this
receiver is active, do not also run a clipboard-export flow. The experiment
cannot prevent a person or unrelated tool from independently copying content.

## Guarantees and limits

- The exact iPhone Use owner and target UDID are checked, and CoreDevice must
  report `wired` before and after payload download and before file commit.
  CoreDevice selects the underlying transport: these are checkpoint checks,
  not a transport lock or proof that a cable cannot change between checks.
- Owner loss, a connection error, explicit cancellation, or the deadline ends
  the receiver. Device subprocesses are bounded by the remaining deadline and
  a 15-second command cap. Handoff settings are unchanged throughout, so there
  is no global setting to restore after disconnection or process failure.
- A baseline transfer ID prevents an existing inbox item from becoming a new
  result. Payload and immutable manifest precede the atomic latest pointer.
  Phone imports are serialized. This is **one file at a time**, not a queue;
  wait for each receipt before sharing another file.
- File names, IDs, manifest size, byte count and digest are validated. Payload
  hashing/copying is chunked. Destination staging is private (0600), flushed,
  checked and atomically linked without overwrite. Existing identical content
  at the same path is accepted; a different file or symlink is refused.
- Acknowledgement or receipt-log failures after commit are explicitly reported
  as post-commit warnings. Full local destination paths are not sent back to
  the phone. No clipboard cache or invoice is a test fixture in this repository.
- The iOS app retains imported documents and receipts in its sandbox for
  inspection. There is no automatic cleanup/retention UI yet. Repeated shares
  may receive an iOS-added filename suffix; cross-name content deduplication,
  large queues, wireless transport, and production lifecycle integration are
  out of scope.

## Tests

```sh
python3 -m unittest discover -s tests -v
bash -n build-install.sh
```

The hardware results and untested boundaries are in [VALIDATION.md](VALIDATION.md).
Tests use fake device operations for failure paths; they do not require a phone
or access any private documents.

For repeatable Debug hardware tests, copy a synthetic file to
`Documents/Fixtures/<basename>` in this app's container, arm the receiver, then
launch with `--usb-bridge-fixture <basename>`. This Debug-only hook invokes the
same importer, restricts its input to that directory, and is absent in Release.
A real share-menu test is still required separately; the hook does not test iOS
sharing UI. Use only synthetic files when publishing test evidence.
