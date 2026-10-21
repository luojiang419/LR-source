import json
from pathlib import Path
import tempfile
import time
import unittest

from mouse_native.interception import ClickRouter,Interception
from mouse_native.mouse_transport import MousePacket
from mouse_remote import MouseRemote
import test_mouse_remote as fixture


def feed(transport,*packets):
    for state,delta in packets:
        packet=MousePacket();packet.state=state;packet.rolling=delta&65535;transport.packets.put(packet)
    transport.signal.set()


class ExtendedRouterTests(unittest.TestCase):
    def test_right_edges_and_mixed_packet_order(self):
        router=ClickRouter();router.select(16)
        self.assertEqual(router.actions(15,4),[])
        self.assertEqual([router.actions(16,s) for s in [4,4,4,8,4]],[['decrease'],[],[],[],['decrease']])
        router.select(16)
        self.assertEqual(router.actions(16,0x405,-120),['increase','decrease','next'])
        self.assertEqual(router.actions(16,0x405,120),['previous'])
    def test_signed_high_resolution_wheel_and_horizontal_ignored(self):
        router=ClickRouter();router.select(16)
        self.assertEqual([router.actions(16,0x400,30) for _ in range(4)],[[],[],[],['previous']])
        self.assertEqual(router.actions(16,0x400,65536-240),['next','next'])
        self.assertEqual(router.actions(16,0x800,120),[])
        router.actions(16,0x400,60);router.select(16);self.assertEqual(router.actions(16,0x400,60),[])


class OptionsTests(unittest.TestCase):
    setUp=fixture.MouseRemoteTests.setUp
    tearDown=fixture.MouseRemoteTests.tearDown
    wait_clicks=fixture.MouseRemoteTests.wait_clicks
    def test_default_disabled_independent_options_and_queue_invalidation(self):
        self.remote.start(self.client,16);self.assertEqual(self.remote.state()['options'],{'right_decrease':False,'wheel_browse':False,'rating_mode':'incremental','auto_enable':False})
        feed(self.transport,(4,0),(8,0),(0x400,-120),(1,0),(2,0));self.wait_clicks(1)
        self.assertEqual([e['action'] for e in self.remote.events],['increase'])
        self.remote.configure({'right_decrease':True},self.client)
        feed(self.transport,(4,0),(4,0),(8,0),(0x400,120));self.wait_clicks(2)
        self.assertEqual([e['action'] for e in self.remote.events],['increase','decrease'])
        self.remote.configure({'wheel_browse':True},self.client);feed(self.transport,(0x400,-240));self.wait_clicks(4)
        self.assertEqual([e['action'] for e in self.remote.events][-2:],['next','next'])
        self.remote.configure({'right_decrease':False,'wheel_browse':False},self.client)
        self.assertEqual([e['action'] for e in self.remote.events],['increase'])
    def test_owner_checks_strict_boolean_and_persistence_without_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'mouse.json';remote=MouseRemote(settings_path=path)
            try:
                self.assertIsNone(remote.bridge);remote.configure({'right_decrease':True});self.assertIsNone(remote.bridge)
                self.assertEqual(json.loads(path.read_text()),{'right_decrease':True,'wheel_browse':False,'rating_mode':'incremental','auto_enable':False})
            finally:remote.close()
            reopened=MouseRemote(settings_path=path)
            try:self.assertEqual(reopened.options,{'right_decrease':True,'wheel_browse':False,'rating_mode':'incremental','auto_enable':False})
            finally:reopened.close()
        self.remote.start(self.client,16)
        with self.assertRaisesRegex(ValueError,'其他窗口'):self.remote.configure({'right_decrease':True},'other-window')
        for value in [{'wheel_browse':1},{'unknown':True},[]]:
            with self.assertRaises(ValueError):self.remote.configure(value,self.client)
    def test_toggle_does_not_replay_old_partial_wheel_or_held_right(self):
        self.remote.start(self.client,16);feed(self.transport,(4,0),(0x400,60))
        end=time.monotonic()+2
        while self.remote.bridge.packets<2 and time.monotonic()<end:time.sleep(.005)
        self.remote.configure({'right_decrease':True,'wheel_browse':True},self.client)
        feed(self.transport,(4,0),(0x400,60));time.sleep(.05);self.assertEqual(self.remote.sequence,0)
        feed(self.transport,(8,0),(4,0));self.wait_clicks(1);self.assertEqual(self.remote.events[0]['action'],'decrease')

    def test_fixed_mode_assigns_both_buttons_and_invalidates_old_actions(self):
        self.remote.start(self.client,16);feed(self.transport,(1,0));self.wait_clicks(1)
        self.remote.configure({'rating_mode':'fixed'},self.client)
        self.assertEqual(list(self.remote.events),[])
        # 切换方案不重置已按住的按键边沿；右键不依赖减星开关。
        feed(self.transport,(1,0),(2,0),(4,0),(4,0),(8,0),(1,0),(2,0));self.wait_clicks(3)
        self.assertEqual([e['action'] for e in self.remote.events],['set_two','set_one'])
        self.assertIn('左键一星、右键两星',self.remote.state()['message'])
        self.remote.configure({'rating_mode':'incremental'},self.client);self.assertEqual(list(self.remote.events),[])
        feed(self.transport,(4,0),(8,0),(1,0),(2,0));self.wait_clicks(4)
        self.assertEqual(self.remote.events[0]['action'],'increase')

    def test_rating_mode_migrates_old_options_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'mouse.json';path.write_text(json.dumps({'right_decrease':True,'wheel_browse':True}),encoding='utf8')
            remote=MouseRemote(settings_path=path)
            try:
                self.assertEqual(remote.options['rating_mode'],'incremental')
                remote.configure({'rating_mode':'fixed'})
                for value in ('toggle',None,1,True,[],{}):
                    with self.assertRaises(ValueError):remote.configure({'rating_mode':value})
                self.assertIsNone(remote.bridge)
            finally:remote.close()
            reopened=MouseRemote(settings_path=path)
            try:self.assertEqual(reopened.options,{'right_decrease':True,'wheel_browse':True,'rating_mode':'fixed','auto_enable':False})
            finally:reopened.close()


if __name__=='__main__':unittest.main()
