"""免重启识别的系统边界隔离；不安装驱动或重载本机鼠标。"""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mouse_native.activation import activate, activation_script
from mouse_native.driver_setup import DriverSetup
from mouse_remote import MouseRemote
from test_mouse_profiles import ProfileTransport, Hotkey, Lease, OLD, NEW


TARGET = r'HID\VID_046D&PID_C534&MI_01&COL01\physical-instance'
DEVICE = {'driver_id': 13, 'hardware': NEW, 'profile': 'logitech-c534'}


class ActivationTests(unittest.TestCase):
    def call(self, snapshots, targets=None, code=0, errors=None):
        snapshots = iter(snapshots)
        discover = Mock(return_value=targets if targets is not None else [TARGET])
        runner = Mock(return_value=code)
        def transport():
            result = Mock()
            result.enumerate.return_value = next(snapshots)
            result.enumeration_errors = errors or []
            return result
        result = activate(discover=discover, runner=runner, transport_factory=transport, pause=lambda _: None)
        return result, discover, runner

    def test_existing_device_never_elevates_or_restarts(self):
        result, discover, runner = self.call([[DEVICE]])
        self.assertTrue(result['usable'])
        discover.assert_not_called();runner.assert_not_called()

    def test_exact_target_reloads_and_waits_for_actual_enumeration(self):
        result, _, runner = self.call([[], [], [DEVICE]])
        runner.assert_called_once_with(TARGET)
        self.assertTrue(result['usable']);self.assertFalse(result['restart_required'])

    def test_unsupported_and_ambiguous_targets_never_reload(self):
        for targets in ([], [r'HID\VID_046D&PID_C534&MI_00&COL01\keyboard'],
                        [r'HID\VID_046D&PID_C999&MI_01&COL01\other'], [TARGET, TARGET+'2']):
            with self.subTest(targets=targets):
                result, _, runner = self.call([[]], targets=targets)
                self.assertFalse(result['usable']);runner.assert_not_called()

    def test_access_denied_and_sharing_violation_do_not_restart(self):
        for error in (5, 32):
            runner = Mock();discover = Mock()
            transport = Mock(enumeration_errors=[{'code':error}]);transport.enumerate.return_value=[]
            with self.assertRaisesRegex(ValueError, '拒绝或被占用'):
                activate(discover=discover,runner=runner,transport_factory=lambda:transport)
            runner.assert_not_called();discover.assert_not_called()

    def test_cancel_does_not_claim_ready(self):
        with self.assertRaisesRegex(ValueError, '未获 Windows 授权'):
            self.call([[]], code=1223)

    def test_command_success_without_device_keeps_restart_guidance(self):
        for code in (0, 3010):
            result, _, _ = self.call([[]]*11, code=code)
            self.assertFalse(result['usable']);self.assertTrue(result['restart_required'])

    def test_windows_requires_restart_but_device_is_now_visible(self):
        result, _, _ = self.call([[], [DEVICE]], code=3010)
        self.assertTrue(result['usable']);self.assertFalse(result['restart_required'])

    def test_rejected_service_identity_change_and_older_windows(self):
        for code in (10,11,12,13,15,16,17):
            result, _, _ = self.call([[]], code=code)
            self.assertFalse(result['usable'])
            self.assertEqual(result['restart_required'],code in (15,16,17))

    def test_worker_keeps_status_pollable_and_does_not_reinstall(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);path=base/'mouse.sys';path.write_bytes(b'verified')
            started=threading.Event();release=threading.Event();manager=Mock()
            def activator():
                started.set();release.wait(5)
                return {'usable':True,'restart_required':False,'message':'已识别'}
            with patch('mouse_native.driver_setup.SUPPORTED_DRIVER_SHA256',hashlib.sha256(b'verified').hexdigest()):
                driver=DriverSetup(manager,base,driver_path=path,activator=activator)
                try:
                    driver.start(activate=True);self.assertTrue(started.wait(2))
                    self.assertEqual(driver.state()['status'],'working')
                    worker=driver.thread;driver.start(activate=True);self.assertIs(driver.thread,worker)
                finally:release.set();driver.thread.join(5)
                self.assertTrue(driver.state()['usable']);manager.ensure.assert_not_called()

    def test_first_install_attempts_activation_and_preserves_cancel_error(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);path=base/'mouse.sys';manager=Mock();manager.root=base
            def installer(*_):path.write_bytes(b'verified');return 0
            with patch('mouse_native.driver_setup.SUPPORTED_DRIVER_SHA256',hashlib.sha256(b'verified').hexdigest()):
                activator=Mock(side_effect=ValueError('未获 Windows 授权'))
                driver=DriverSetup(manager,base,installer,path,activator)
                with self.assertRaisesRegex(ValueError,'未获 Windows 授权'):driver.ensure()
                self.assertTrue(driver.state()['installed']);self.assertEqual(driver.state()['status'],'error')
                activator.side_effect=None;activator.return_value={'usable':True,'restart_required':False,'message':'已识别'}
                driver.ensure(activate=True);self.assertTrue(driver.state()['usable'])
                manager.ensure.assert_called_once_with(['driver'])

    def test_remote_closes_bridge_but_holds_lease_and_protects_active_owner(self):
        lease=Lease();lease.close=Mock();bridge=Mock()
        remote=MouseRemote(lease_factory=lambda:lease,legacy_guard=lambda:None,hotkey_factory=Hotkey)
        try:
            remote.bridge=bridge
            action=Mock(return_value={'usable':True})
            remote.activate_driver(action)
            bridge.close.assert_called_once();lease.close.assert_not_called()
            self.assertIsNone(remote.bridge);self.assertIs(remote.lease,lease)
            remote.owner='active';action.reset_mock()
            self.assertTrue(remote.activate_driver(action)['usable']);action.assert_not_called()
            remote.owner=None
        finally:remote.close()

    def test_legacy_or_other_process_blocks_before_system_action(self):
        for option in ('legacy','lease'):
            remote=MouseRemote(legacy_guard=Mock(side_effect=ValueError('占用')) if option=='legacy' else lambda:None,
                               lease_factory=Mock(side_effect=ValueError('占用')))
            action=Mock()
            try:
                with self.assertRaisesRegex(ValueError,'占用'):remote.activate_driver(action)
                action.assert_not_called()
            finally:remote.close()


if __name__=='__main__':unittest.main()
