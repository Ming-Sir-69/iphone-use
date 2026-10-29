import importlib.util
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).parents[1] / 'mac/receive.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

def metadata(data=b'file bytes', **changes):
    return {'version': 1, 'id': str(uuid.uuid4()), 'name': 'invoice.pdf', 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), **changes}

class FakeDevice:
    def __init__(self, session, meta, data):
        self.session, self.meta, self.data = session, meta, data
        self.reads = 0
        self.released = False
        self.stale = False
        self.corrupt_manifest = False
        self.ack_failure = False
        self.release_after_copy = False
        self.payload_copies = 0

    def guard(self):
        self.session.check()
        if self.released:
            raise b.TransferError('lease ended')

    def latest(self, path):
        self.reads += 1
        if self.reads == 1:
            return {'version': 1, 'id': 'previous'}
        if self.stale:
            self.session.deadline = 0
            return {'version': 1, 'id': 'previous'}
        return self.meta

    def copy(self, direction, source, target):
        if source.endswith('manifest.json'):
            value = {**self.meta, 'name': 'different.pdf'} if self.corrupt_manifest else self.meta
            target.write_text(json.dumps(value))
        else:
            self.payload_copies += 1
            target.write_bytes(self.data)
            if self.release_after_copy:
                self.released = True

    def ack(self, receipt):
        if self.ack_failure:
            raise b.TransferError('ack connection lost')

class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self.dest = self.root / 'destination'
        self.args = types.SimpleNamespace(destination=self.dest, max_bytes=64*1024*1024, expect_sha256=None)
        self.session = b.Session(self.root / 'state', 10)
        self.device = FakeDevice(self.session, metadata(), b'file bytes')
        self.events = []

    def receive(self):
        with self.session.acquire():
            return b.receive(self.args, self.session, self.device, self.scratch, self.events.append)

    def assert_no_commit(self):
        self.assertFalse((self.dest / 'invoice.pdf').exists())
        self.assertEqual(list(self.dest.glob('*.partial')), [])
        self.assertFalse(b.Session.status(self.session.root)['active'])

    def test_success_and_private_atomic_file(self):
        result = self.receive()
        self.assertTrue(result['committed'])
        self.assertTrue(result['phone_ack'])
        self.assertEqual((self.dest/'invoice.pdf').read_bytes(), b'file bytes')
        self.assertEqual((self.dest/'invoice.pdf').stat().st_mode & 0o777, 0o600)
        self.assertFalse(b.Session.status(self.session.root)['active'])

    def test_ack_failure_preserves_committed_result(self):
        self.device.ack_failure = True
        result = self.receive()
        self.assertTrue(result['committed'])
        self.assertFalse(result['phone_ack'])
        self.assertTrue((self.dest/'invoice.pdf').is_file())

    def test_receipt_failure_preserves_committed_result(self):
        real = b.atomic_json
        def write(path, value):
            if path.parent.name == 'receipts': raise OSError('disk full')
            real(path, value)
        with patch.object(b, 'atomic_json', side_effect=write):
            result = self.receive()
        self.assertTrue(result['committed'])
        self.assertIn('receipt_error', result)

    def test_stale_pointer_times_out(self):
        self.device.stale = True
        with self.assertRaisesRegex(b.TransferError, 'timed out'): self.receive()
        self.assertEqual(self.device.payload_copies, 0)
        self.assert_no_commit()

    def test_payload_corruption(self):
        self.device.data = b'changed'
        with self.assertRaisesRegex(b.TransferError, 'integrity'): self.receive()
        self.assert_no_commit()

    def test_oversized_manifest_rejected_before_payload(self):
        self.args.max_bytes = 1
        with self.assertRaisesRegex(b.TransferError, 'oversized'): self.receive()
        self.assertEqual(self.device.payload_copies, 0)
        self.assert_no_commit()

    def test_immutable_manifest_mismatch(self):
        self.device.corrupt_manifest = True
        with self.assertRaisesRegex(b.TransferError, 'manifest differs'): self.receive()
        self.assertEqual(self.device.payload_copies, 0)
        self.assert_no_commit()

    def test_lease_end_before_commit(self):
        self.device.release_after_copy = True
        with self.assertRaisesRegex(b.TransferError, 'lease ended'): self.receive()
        self.assert_no_commit()

    def test_expected_hash_mismatch(self):
        self.args.expect_sha256 = '0'*64
        with self.assertRaisesRegex(b.TransferError, 'expected SHA'): self.receive()
        self.assert_no_commit()

    def test_existing_different_file_preserved(self):
        self.dest.mkdir()
        target = self.dest/'invoice.pdf'
        target.write_bytes(b'original')
        with self.assertRaisesRegex(b.TransferError, 'refusing overwrite'): self.receive()
        self.assertEqual(target.read_bytes(), b'original')

    def test_existing_identical_file_is_idempotent(self):
        self.dest.mkdir()
        (self.dest/'invoice.pdf').write_bytes(b'file bytes')
        self.assertEqual(self.receive()['result'], 'already_identical')

    def test_symlink_target_refused(self):
        self.dest.mkdir()
        victim = self.root/'other'
        victim.write_bytes(b'file bytes')
        (self.dest/'invoice.pdf').symlink_to(victim)
        with self.assertRaisesRegex(b.TransferError, 'symlink'): self.receive()
        self.assertEqual(victim.read_bytes(), b'file bytes')

    def test_race_created_target_never_overwritten(self):
        real = os.link
        def link(stage, target):
            target.write_bytes(b'other writer')
            return real(stage, target)
        with patch.object(b.os, 'link', side_effect=link), self.assertRaises(FileExistsError):
            self.receive()
        self.assertEqual((self.dest/'invoice.pdf').read_bytes(), b'other writer')
        self.assertEqual(list(self.dest.glob('*.partial')), [])

    def test_publish_disk_error_cleans_partial(self):
        with patch.object(b.os, 'link', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.receive()
        self.assert_no_commit()

    def test_multi_chunk_and_empty_file(self):
        for data in (b'', b'abcdef' * (400000)):
            with self.subTest(size=len(data)):
                self.device.data = data
                self.device.meta = metadata(data)
                self.device.reads = 0
                (self.dest/'invoice.pdf').unlink(missing_ok=True)
                self.receive()
                self.assertEqual((self.dest/'invoice.pdf').read_bytes(), data)

class MetadataTests(unittest.TestCase):
    def test_invalid_fields(self):
        for change in ({'version': 2}, {'id': '../bad'}, {'name': '../bad'}, {'name': 'a\\b'},
                       {'name': '\x00'}, {'name': '.'}, {'name': 'x'*241}, {'bytes': True},
                       {'bytes': -1}, {'sha256': 'wrong'}):
            with self.subTest(change=change), self.assertRaises(b.TransferError):
                b.validate_manifest(metadata(**change), 1024)

    def test_json_metadata_limit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'metadata'
            path.write_text(' ' * 65537)
            with self.assertRaises(b.TransferError): b.read_json(path)

class SessionTests(unittest.TestCase):
    def test_lock_cancel_cleanup_and_stale_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first, second = b.Session(root, 10), b.Session(root, 10)
            with first.acquire():
                self.assertTrue(b.Session.status(root)['active'])
                with self.assertRaisesRegex(b.TransferError, 'Another receiver'):
                    with second.acquire(): pass
                self.assertTrue(b.Session.cancel(root))
                with self.assertRaises(b.Cancelled): first.check()
            self.assertFalse(b.Session.status(root)['active'])
            (root/'session.json').write_text('{"pid":1}')
            self.assertFalse(b.Session.status(root)['active'])
            self.assertFalse(b.Session.cancel(root))

    def test_old_cancel_token_does_not_stop_new_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/('cancel-'+str(uuid.uuid4()))).touch()
            with b.Session(root, 10).acquire() as session: self.assertGreater(session.check(), 0)

class DeviceTests(unittest.TestCase):
    def test_guard_transport_owner_and_target(self):
        for transport, owner, udid, accepted in [('wired','me','phone',True),
                ('wireless','me','phone',False),('wired','other','phone',False),('wired','me','other',False)]:
            with self.subTest(transport=transport,owner=owner,udid=udid), tempfile.TemporaryDirectory() as d:
                root=Path(d)
                args=types.SimpleNamespace(device='phone',owner='me',instance='default')
                dev=b.Device(args,b.Session(root,10),root)
                def command(argv, *rest):
                    if argv[0]=='iphone-use':return json.dumps({'drivable':True,'owner':owner,'udid':udid})
                    (root/'device.json').write_text(json.dumps({'result':{'connectionProperties':{'transportType':transport}}}))
                    return ''
                with patch.object(dev,'command',side_effect=command):
                    if accepted: dev.guard()
                    else:
                        with self.assertRaises(b.TransferError):dev.guard()

    def test_command_timeout_bounded_and_cancel_checked(self):
        with tempfile.TemporaryDirectory() as d:
            session=b.Session(Path(d),2)
            dev=b.Device(None,session,Path(d))
            with patch.object(b.subprocess,'run',side_effect=subprocess.TimeoutExpired('cmd',2)) as run:
                with self.assertRaisesRegex(b.TransferError,'timed out'):dev.command(['cmd'])
                self.assertLessEqual(run.call_args.kwargs['timeout'],2)


class CliTests(unittest.TestCase):
    def test_busy_error_is_json_at_actual_entrypoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            key = hashlib.sha256(json.dumps(['test-device','test-bundle','default']).encode()).hexdigest()[:24]
            with b.Session(root/key, 10).acquire():
                command = [sys.executable, str(Path(b.__file__)), 'receive', '--device', 'test-device',
                           '--bundle', 'test-bundle', '--owner', 'test-owner',
                           '--destination', str(root/'out'), '--state-dir', str(root)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stderr)['event'], 'error')
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((root/'out').exists())

    def test_idle_status_does_not_contact_device(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, str(Path(b.__file__)), 'status',
                '--device', 'test-device', '--bundle', 'test-bundle', '--state-dir', d],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(json.loads(result.stdout)['active'])

if __name__ == '__main__': unittest.main()
