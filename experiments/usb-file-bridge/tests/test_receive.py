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

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).parents[1] / 'mac/common.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

def metadata(data=b'file bytes', **changes):
    return {'version': 1, 'id': str(uuid.uuid4()), 'name': 'invoice.pdf', 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), **changes}

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
            key = hashlib.sha256(json.dumps(['test-device','test-bundle.xctrunner','default']).encode()).hexdigest()[:24]
            with b.Session(root/key, 10).acquire():
                command = [sys.executable, str(Path(b.__file__).with_name('runner_files.py')), 'receive', '--device', 'test-device',
                           '--bundle', 'test-bundle.xctrunner', '--owner', 'test-owner',
                           '--destination', str(root/'out'), '--filename', 'sample.pdf', '--state-dir', str(root)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stderr)['event'], 'error')
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((root/'out').exists())

    def test_idle_status_does_not_contact_device(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, str(Path(b.__file__).with_name('runner_files.py')), 'status',
                '--device', 'test-device', '--bundle', 'test-bundle.xctrunner', '--state-dir', d],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(json.loads(result.stdout)['active'])

if __name__ == '__main__': unittest.main()
