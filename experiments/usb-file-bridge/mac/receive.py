#!/usr/bin/env python3
"""Explicit, one-file, wired-only CoreDevice inbox experiment (no clipboard access)."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

CHUNK = 1024 * 1024

class TransferError(RuntimeError):
    pass

class Cancelled(TransferError):
    pass

def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.json-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def read_json(path):
    if path.stat().st_size > 64 * 1024:
        raise TransferError('JSON metadata exceeds 64 KiB')
    return json.loads(path.read_text())

def validate_manifest(value, max_bytes):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise TransferError('Unsupported transfer manifest')
    ident = value.get('id')
    if not isinstance(ident, str):
        raise TransferError('Missing transfer id')
    try:
        if str(uuid.UUID(ident)).lower() != ident.lower():
            raise ValueError()
    except ValueError:
        raise TransferError('Invalid transfer id') from None
    name = value.get('name')
    if (not isinstance(name, str) or name in ('', '.', '..')
            or any(c in name for c in '/\\') or len(name.encode('utf-8')) > 240
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise TransferError('Invalid file name')
    size = value.get('bytes')
    if type(size) is not int or not 0 <= size <= max_bytes:
        raise TransferError('Invalid or oversized file')
    if not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
        raise TransferError('Invalid SHA-256')
    return {k: value[k] for k in ('version', 'id', 'name', 'bytes', 'sha256')}

def digest_file(path, maximum):
    digest = hashlib.sha256()
    total = 0
    with path.open('rb') as stream:
        while data := stream.read(CHUNK):
            total += len(data)
            if total > maximum:
                raise TransferError('File exceeds size limit')
            digest.update(data)
    return total, digest.hexdigest()

class Session:
    """OS lock is authoritative; cancellation is token-based, never kill-by-PID."""
    def __init__(self, root, seconds):
        self.root = root
        self.seconds = seconds
        self.token = str(uuid.uuid4())
        self.deadline = time.monotonic() + seconds
        self.lock = None

    @contextlib.contextmanager
    def acquire(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransferError('Another receiver is active for this device/bundle/instance') from None
            self.lock = lock
            atomic_json(self.root / 'session.json', {
                'token': self.token, 'pid': os.getpid(), 'route': 'usb',
                'expires': time.time() + self.seconds, 'system_clipboard': 'unchanged',
            })
            try:
                yield self
            finally:
                (self.root / 'session.json').unlink(missing_ok=True)
                (self.root / ('cancel-' + self.token)).unlink(missing_ok=True)
                self.lock = None

    def check(self):
        if (self.root / ('cancel-' + self.token)).exists():
            raise Cancelled('USB receive cancelled')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError('USB receive timed out')
        return remaining

    @staticmethod
    def status(root):
        normal = {'active': False, 'route': 'default', 'system_clipboard': 'unchanged'}
        if not root.exists():
            return normal
        with (root / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return normal  # stale state/PID cannot keep a route active
            except BlockingIOError:
                try:
                    state = read_json(root / 'session.json')
                    token = str(uuid.UUID(state['token']))
                    return {**state, 'active': True,
                            'cancelling': (root / ('cancel-' + token)).exists()}
                except (OSError, ValueError, KeyError):
                    return {'active': True, 'route': 'usb', 'starting': True}

    @staticmethod
    def cancel(root):
        state = Session.status(root)
        if not state.get('active'):
            return False
        if 'token' not in state:
            raise TransferError('Receiver is starting; retry stop shortly')
        token = str(uuid.UUID(state['token']))
        (root / ('cancel-' + token)).touch(mode=0o600, exist_ok=True)
        return True

class Device:
    def __init__(self, args, session, scratch):
        self.args, self.session, self.scratch = args, session, scratch

    def command(self, args, limit=15):
        timeout = min(limit, self.session.check())
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransferError('Device command timed out; no success inferred') from None
        self.session.check()
        if result.returncode:
            raise TransferError(result.stderr.strip() or result.stdout.strip() or 'Device command failed')
        return result.stdout

    def guard(self):
        state = json.loads(self.command(['iphone-use', 'status', '--instance', self.args.instance, '--json'], 10))
        if not state.get('drivable') or state.get('owner') != self.args.owner:
            raise TransferError('iPhone Use control lease ended or belongs to another caller')
        if state.get('udid') != self.args.device:
            raise TransferError('iPhone Use instance targets a different UDID')
        path = self.scratch / 'device.json'
        self.command(['xcrun', 'devicectl', 'device', 'info', 'details', '--device', self.args.device,
                      '--timeout', '10', '--json-output', str(path)])
        obj = read_json(path)['result']
        conn = obj.get('connectionProperties') or obj.get('properties', {}).get('connection', {})
        if conn.get('transportType') != 'wired':
            raise TransferError('Wired transport required; refusing wireless fallback')

    def copy(self, direction, source, destination):
        self.command(['xcrun', 'devicectl', 'device', 'copy', direction,
                      '--device', self.args.device, '--domain-type', 'appDataContainer',
                      '--domain-identifier', self.args.bundle, '--source', str(source),
                      '--destination', str(destination), '--timeout', '15'])

    def latest(self, path):
        self.copy('from', 'Documents/latest.json', path)
        value = read_json(path)
        if not isinstance(value, dict) or value.get('version') != 1:
            raise TransferError('Launch the inbox app once to initialize its versioned manifest')
        return value

    def ack(self, receipt):
        path = self.scratch / 'ack.json'
        # Do not copy local filesystem paths or diagnostic output back to the phone.
        atomic_json(path, {k: receipt[k] for k in ('id', 'name', 'bytes', 'sha256')})
        self.guard()
        self.copy('to', path, 'Documents/receipt.json')

def commit_file(source, meta, destination, session, guard):
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / meta['name']
    if target.is_symlink():
        raise TransferError('Refusing symlink destination')
    if target.exists():
        if not target.is_file() or digest_file(target, meta['bytes']) != (meta['bytes'], meta['sha256']):
            raise TransferError('Destination differs; refusing overwrite')
        guard()
        return target, 'already_identical'
    fd, name = tempfile.mkstemp(prefix='.usb-', suffix='.partial', dir=destination)
    stage = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            while chunk := incoming.read(CHUNK):
                session.check()
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest_file(stage, meta['bytes']) != (meta['bytes'], meta['sha256']):
            raise TransferError('Staging file integrity mismatch')
        guard()
        # link() atomically refuses a race-created destination, unlike replace().
        os.link(stage, target)
        return target, 'copied'
    finally:
        stage.unlink(missing_ok=True)

def receive(args, session, device, scratch, emit):
    device.guard()
    baseline = device.latest(scratch / 'before.json').get('id')
    emit({'event': 'armed', 'route': 'usb'})
    while True:
        session.check()
        device.guard()
        latest = device.latest(scratch / 'latest.json')
        if latest.get('id') and latest['id'] != baseline:
            meta = validate_manifest(latest, args.max_bytes)
            break
        time.sleep(min(0.5, session.check()))
    manifest = scratch / 'manifest.json'
    prefix = 'Documents/Transfers/' + meta['id']
    device.copy('from', prefix + '/manifest.json', manifest)
    if validate_manifest(read_json(manifest), args.max_bytes) != meta:
        raise TransferError('Immutable manifest differs from latest pointer')
    payload = scratch / 'payload'
    device.copy('from', prefix + '/payload', payload)
    if digest_file(payload, args.max_bytes) != (meta['bytes'], meta['sha256']):
        raise TransferError('Phone/Mac integrity mismatch')
    if args.expect_sha256 and meta['sha256'] != args.expect_sha256:
        raise TransferError('Received file differs from expected SHA-256')
    target, result = commit_file(payload, meta, args.destination, session, device.guard)
    receipt = {**meta, 'event': 'received', 'committed': True, 'result': result,
               'destination': str(target.resolve()), 'transport': 'wired', 'clipboard_used': False}
    # Commit is irreversible: later acknowledgement errors must not report a failed copy.
    try:
        device.ack(receipt)
        receipt['phone_ack'] = True
    except Exception as error:
        receipt.update(phone_ack=False, phone_ack_error=str(error))
    try:
        receipts = session.root / 'receipts'
        receipts.mkdir(exist_ok=True, mode=0o700)
        atomic_json(receipts / (meta['id'] + '.json'), receipt)
    except Exception as error:
        receipt['receipt_error'] = str(error)
    emit(receipt)
    return receipt

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['receive', 'status', 'stop'])
    p.add_argument('--device', required=True, help='Exact UDID; must match the iPhone Use instance')
    p.add_argument('--bundle', required=True, help='Bundle identifier used to sign/install the inbox app')
    p.add_argument('--instance', default='default')
    p.add_argument('--owner', default=os.environ.get('IPHONE_USE_OWNER'), help='Exact caller control lease owner; required for receive')
    p.add_argument('--destination', type=Path)
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.iphone-use' / 'usb-file-bridge')
    p.add_argument('--timeout', type=float, default=180)
    p.add_argument('--max-bytes', type=int, default=64 * 1024 * 1024)
    p.add_argument('--expect-sha256')
    return p

def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 1 <= args.timeout <= 3600 or not 0 <= args.max_bytes <= 64 * 1024 * 1024:
        p.error('timeout must be 1–3600 seconds; max-bytes 0–67108864')
    if args.expect_sha256 and not re.fullmatch('[0-9a-f]{64}', args.expect_sha256):
        p.error('expect-sha256 must contain 64 lowercase hex digits')
    key = hashlib.sha256(json.dumps([args.device, args.bundle, args.instance]).encode()).hexdigest()[:24]
    root = args.state_dir.expanduser() / key
    emit = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
    if args.action == 'status':
        emit(Session.status(root)); return
    if args.action == 'stop':
        emit({'cancel_requested': Session.cancel(root), 'system_clipboard': 'unchanged'}); return
    if not args.destination or not args.owner:
        p.error('receive requires --destination and --owner (or IPHONE_USE_OWNER)')
    args.destination = args.destination.expanduser().resolve()
    session = Session(root, args.timeout)
    def cancelled(*_):
        raise Cancelled('USB receive interrupted')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancelled)
    acquired = False
    try:
        with session.acquire():
            acquired = True
            with tempfile.TemporaryDirectory(prefix='receive-', dir=root) as temp:
                scratch = Path(temp)
                receive(args, session, Device(args, session, scratch), scratch, emit)
    finally:
        if acquired:
            emit({'event': 'session-ended', 'system_clipboard': 'unchanged',
                  'receiver': Session.status(root)})

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'event': 'error', 'detail': str(error), 'system_clipboard': 'unchanged'}), file=sys.stderr)
        sys.exit(1)
