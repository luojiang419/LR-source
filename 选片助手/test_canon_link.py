"""隔离 Canon 配置与照片，验证自动登记、恢复和联机通知。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from unittest.mock import patch

from PIL import Image
from canon_link import CanonLink,LINK_VALUES,SAVE_VALUES,read_settings,discover_installation
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class CanonLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'联机 照片';self.photos.mkdir();self.app=self.base/'末帧选片.exe';self.app.write_bytes(b'MZ')
        self.store=Store(self.base/'data',ROOT);self.watch=FolderWatch(self.store,autostart=False)
        self.config=self.base/'新用户/3.20.21.1/user.config';self.defaults=self.base/'EOS Utility 3.exe.config'
        entries={'LinkedApp':'','IsLinkedAppOther':'False','IsLinkedAppDPP':'True','IsLinkedAppNA':'False',
                 'IsLinkedAppVR':'False','IsLinkedAppIBX':'False',**{k:'False' for k in LINK_VALUES if k.startswith('Notify')},
                 'SaveFolder':str(self.photos),**SAVE_VALUES,'FolderNameSeparator':'0','FileCustomizeIndex':'3','Unrelated':'原设置'}
        section='System, Version=2.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089'
        self.defaults.write_text('<configuration><configSections><sectionGroup name="userSettings" type="System.Configuration.UserSettingsGroup, '+section+'"><section name="EOSUtility.AppSettings" type="System.Configuration.ClientSettingsSection, '+section+'" allowExeDefinition="MachineToLocalUser" requirePermission="false" /></sectionGroup></configSections><userSettings><EOSUtility.AppSettings>'+''.join(f'<setting name="{k}" serializeAs="String"><value>{v}</value></setting>' for k,v in entries.items())+'</EOSUtility.AppSettings></userSettings></configuration>',encoding='utf8')
        self.install={'executable':'EOS Utility 3.exe','version':'3.20.21.1','config':str(self.config),'defaults':str(self.defaults)}
        self.running=False
        self.service=CanonLink(self.store,self.watch,self.app,discover=lambda:self.install,running=lambda:self.running,autostart=False)
        self.service.configure(True)  # 既有功能测试均从用户明确启用开始。

    def tearDown(self):self.service.close();self.watch.close();self.temp.cleanup()

    def picture(self,folder=None,name='第一张.JPG'):
        folder=folder or self.photos;folder.mkdir(parents=True,exist_ok=True);path=folder/name
        Image.new('RGB',(600,400),'green').save(path);return path

    def existing_config(self):
        self.config.parent.mkdir(parents=True,exist_ok=True);self.config.write_bytes(self.defaults.read_bytes())

    def test_restart_follows_registered_eos_without_rewriting_legacy_destination(self):
        self.existing_config();self.service.configure_destination(str(self.photos));photo=self.picture()
        profile=self.config.read_bytes();preferences=self.service.preferences.read_bytes()
        backups={p.name:p.read_bytes() for p in self.service.folder.glob('registration-*')}
        self.assertTrue(json.loads(preferences)['enabled']);self.watch.close()
        for running in [True,False]:
            watch=FolderWatch(self.store,autostart=False)
            with patch('canon_link.discover_installation',return_value=self.install) as discover:
                idle=CanonLink(self.store,watch,self.app,discover=discover,running=lambda:running,autostart=False)
                try:
                    idle.tick();idle.tick()
                    self.assertTrue(idle.state()['enabled']);self.assertTrue(watch.state()['enabled'])
                    self.assertTrue(idle.state()['follow_eos']);self.assertEqual(Path(watch.state()['folder']),self.photos)
                    self.assertEqual(idle.state()['destination'],str(self.photos));self.assertIn('跟随',idle.state()['message'])
                    self.assertFalse(idle.state()['pending']);self.assertNotIn('等待生效',idle.state()['message'])
                    self.assertTrue(idle.receive([str(photo)])['accepted']);self.assertTrue(discover.called)
                    self.assertEqual(self.config.read_bytes(),profile);self.assertEqual(idle.preferences.read_bytes(),preferences)
                    self.assertEqual({p.name:p.read_bytes() for p in idle.folder.glob('registration-*')},backups)
                finally:idle.close();watch.close()

    def test_fresh_start_does_not_create_eos_profile_before_user_activation(self):
        with patch('canon_link.discover_installation',return_value=self.install) as discover:
            idle=CanonLink(self.store,self.watch,self.app,discover=discover,running=lambda:False,autostart=False)
            try:
                idle.tick();self.assertFalse(self.config.exists());discover.assert_called_once()
                self.assertFalse(idle.state()['enabled'])
                idle.configure(True);idle.discover=lambda:self.install;idle.tick()
                self.assertTrue(self.config.exists());self.assertTrue(idle.state()['registered'])
            finally:idle.close()

    def auto_service(self):
        service=CanonLink(self.store,self.watch,self.app,discover=lambda:self.install,running=lambda:self.running,autostart=False)
        self.addCleanup(service.close)
        return service

    def registered_profile(self,folder=None):
        self.existing_config();root=ET.parse(self.config)
        for key,value in {'LinkedApp':str(self.app),'IsLinkedAppOther':'True','IsLinkedAppDPP':'False','SaveFolder':str(folder or self.photos)}.items():
            root.find(f'.//setting[@name="{key}"]/value').text=value
        root.write(self.config,encoding='utf-8')

    def test_auto_follow_ignores_old_destination_and_catches_nested_and_new_date_folders(self):
        self.service.destination=str(self.base/'不存在的旧路径');self.service._persist()
        self.running=True;self.registered_profile();before=self.config.read_bytes();idle=self.auto_service()
        idle.tick();self.assertTrue(idle.state()['follow_eos']);generation=self.watch.state()['generation']
        first=self.picture(self.photos/'存储/2026-10-23');idle.receive([str(first)])
        self.assertNotEqual(self.watch.state()['generation'],generation)
        first_generation=self.watch.state()['generation'];idle.receive([str(first)]);idle.tick()
        self.assertEqual(self.watch.state()['generation'],first_generation)
        second=self.picture(self.photos/'存储/2026-10-24');idle.receive([str(second)]);idle.tick()
        self.assertNotEqual(self.watch.state()['generation'],first_generation)
        self.assertEqual(Path(self.watch.state()['folder']),second.parent);self.assertTrue(self.watch.state()['recursive'])
        target=self.base/'新的EOS保存';target.mkdir();root=ET.parse(self.config)
        root.find('.//setting[@name="SaveFolder"]/value').text=str(target);root.write(self.config,encoding='utf-8')
        changed=self.config.read_bytes();idle.tick();self.assertEqual(Path(self.watch.state()['folder']),second.parent)
        new=self.picture(target);idle.receive([str(new)]);idle.tick();self.assertEqual(Path(self.watch.state()['folder']),target)
        self.assertEqual(self.config.read_bytes(),changed);self.assertNotEqual(changed,before)
        self.assertEqual(read_settings(self.config)['FileCustomizeIndex'],'3')

    def test_disabled_restart_stays_idle_until_new_photo_notification(self):
        self.service.configure_destination(str(self.photos));before=self.config.read_bytes();idle=self.auto_service();idle.tick()
        idle.pause();self.watch.stop();idle.tick();self.assertFalse(self.watch.state()['enabled'])
        idle.configure(True,True);idle.tick();self.assertTrue(self.watch.state()['enabled'])
        idle.configure(False,True);idle.tick();self.assertFalse(self.watch.state()['enabled'])
        self.assertEqual(self.config.read_bytes(),before)
        restarted=self.auto_service();restarted.tick();self.assertFalse(restarted.state()['enabled'])
        self.assertTrue(restarted.receive([str(self.picture())])['accepted']);self.assertEqual(self.config.read_bytes(),before)

    def test_auto_follow_does_not_take_over_manual_watch(self):
        self.registered_profile();manual=self.base/'手动目录';manual.mkdir();self.watch.start(str(manual))
        generation=self.watch.state()['generation'];idle=self.auto_service();idle.tick()
        self.assertFalse(idle.state()['enabled'])
        self.assertEqual(self.watch.state()['generation'],generation)
        self.watch.stop();idle.tick();self.assertTrue(idle.state()['enabled'])

    def test_first_notification_overrides_historic_disable_and_restored_manual_watch(self):
        self.existing_config();profile=self.config.read_bytes();self.service.configure(False,True)
        manual=self.base/'旧手动监控';manual.mkdir();self.watch.start(str(manual))
        idle=self.auto_service();preferences=idle.preferences.read_bytes();idle.tick()
        self.assertFalse(idle.state()['enabled']);self.assertEqual(Path(self.watch.state()['folder']),manual)
        photo=self.picture();result=idle.receive([str(photo)])
        self.assertTrue(result['accepted']);self.assertEqual(Path(result['folder']),self.photos)
        first=self.watch.state();self.assertEqual(first['source'],'canon');self.assertTrue(first['canon_event'])
        self.assertTrue(idle.state()['follow_eos']);self.assertTrue(idle.state()['enabled'])
        idle.receive([str(photo)]);second=self.watch.wait_state(first['event_id'],timeout=.01)
        self.assertGreater(second['event_id'],first['event_id'])
        self.assertNotEqual(second['canon_event'],first['canon_event'])
        self.assertEqual(second['generation'],first['generation'])
        self.assertEqual(self.config.read_bytes(),profile);self.assertEqual(idle.preferences.read_bytes(),preferences)
        idle.configure(False,True);self.assertFalse(idle.receive([str(photo)])['accepted'])
        self.assertFalse(self.watch.state()['enabled'])

    def test_notification_starts_auto_follow_before_eos_profile_is_flushed(self):
        self.existing_config();before=self.config.read_bytes();self.running=True;idle=self.auto_service();idle.tick()
        self.assertFalse(idle.state()['enabled']);photo=self.picture(self.photos/'存储/即时拍摄')
        self.assertTrue(idle.receive([str(photo)])['accepted']);idle.tick()
        self.assertTrue(idle.state()['enabled']);self.assertEqual(Path(self.watch.state()['folder']),photo.parent)
        self.assertTrue(self.watch.state()['canon_event'])
        self.assertEqual(self.config.read_bytes(),before)

    def test_auto_follow_stops_when_eos_selects_another_application(self):
        self.registered_profile();self.running=True;idle=self.auto_service();idle.tick();self.assertTrue(self.watch.state()['enabled'])
        idle.receive([str(self.picture())])
        root=ET.parse(self.config);root.find('.//setting[@name="LinkedApp"]/value').text='D:/Other.exe';root.write(self.config,encoding='utf-8')
        before=self.config.read_bytes();idle.tick();self.assertFalse(self.watch.state()['enabled']);self.assertFalse(idle.state()['enabled'])
        self.assertEqual(self.config.read_bytes(),before)

    def test_notification_survives_stale_dpp_profile_after_previously_registered(self):
        self.registered_profile();self.running=True;idle=self.auto_service();idle.tick()
        root=ET.parse(self.config)
        root.find('.//setting[@name="IsLinkedAppOther"]/value').text='False'
        root.find('.//setting[@name="IsLinkedAppDPP"]/value').text='True'
        root.write(self.config,encoding='utf-8');before=self.config.read_bytes()
        photo=self.picture(self.photos/'实际拍摄');idle.receive([str(photo)])
        generation=self.watch.state()['generation']
        for _ in range(4):idle.tick()
        self.assertTrue(idle.state()['enabled']);self.assertTrue(self.watch.state()['enabled'])
        self.assertEqual(Path(self.watch.state()['folder']),photo.parent)
        self.assertEqual(self.watch.state()['generation'],generation)
        self.assertEqual(self.config.read_bytes(),before)

    def test_same_session_notifications_do_not_repeat_activation_work(self):
        self.registered_profile();self.running=True;idle=self.auto_service();photo=self.picture()
        with patch.object(self.store,'activate',wraps=self.store.activate) as activate:
            idle.receive([str(photo)]);self.assertEqual(activate.call_count,1)
            idle.receive([str(photo)]);idle.tick();idle.tick()
            self.assertEqual(activate.call_count,1)
            other=self.base/'手动浏览';other.mkdir();other_sid=self.store.ensure_live_session(other)
            self.store.activate(other_sid);idle.receive([str(photo)])
            self.assertEqual(activate.call_count,3)
            self.assertEqual(self.store.active_id(),self.watch.state()['session_id'])

    def test_live_notification_directory_wins_conflicting_and_offline_disk_root(self):
        self.registered_profile();self.running=True;idle=self.auto_service();idle.tick()
        photo=self.picture(self.base/'内存中实际保存/日期');idle.receive([str(photo)])
        generation=self.watch.state()['generation']
        for target in (self.photos,self.base/'配置中的离线共享'):
            root=ET.parse(self.config);root.find('.//setting[@name="SaveFolder"]/value').text=str(target)
            root.write(self.config,encoding='utf-8');before=self.config.read_bytes()
            for _ in range(3):idle.tick()
            self.assertTrue(self.watch.state()['enabled'])
            self.assertEqual(Path(self.watch.state()['folder']),photo.parent)
            self.assertEqual(self.watch.state()['generation'],generation)
            self.assertEqual(Path(idle.state()['save_folder']),photo.parent)
            self.assertEqual(self.config.read_bytes(),before)
        second=self.picture(self.base/'再次切换/日期');idle.receive([str(second)]);idle.tick()
        self.assertEqual(Path(self.watch.state()['folder']),second.parent)

    def test_profile_directory_change_without_live_notification_still_follows_disk(self):
        self.registered_profile();self.running=True;idle=self.auto_service();idle.tick()
        target=self.base/'新设置目录';target.mkdir()
        self.registered_profile(target);idle.tick()
        self.assertEqual(Path(self.watch.state()['folder']),target)

    def test_stale_profile_authority_ends_when_eos_exits(self):
        self.existing_config();self.running=True;idle=self.auto_service()
        idle.receive([str(self.picture())]);idle.tick();self.assertTrue(self.watch.state()['enabled'])
        self.running=False;idle.tick();self.assertFalse(self.watch.state()['enabled'])

    def test_later_link_selection_change_stops_after_stale_profile_notification(self):
        self.existing_config();self.running=True;idle=self.auto_service()
        idle.receive([str(self.picture())]);idle.tick();self.assertTrue(self.watch.state()['enabled'])
        self.registered_profile();idle.tick()
        root=ET.parse(self.config);root.find('.//setting[@name="IsLinkedAppOther"]/value').text='False'
        root.write(self.config,encoding='utf-8');idle.tick()
        self.assertFalse(self.watch.state()['enabled'])

    def test_auto_follow_recovers_after_offline_root_and_uses_default_subfolder_settings(self):
        missing=self.base/'稍后上线';self.registered_profile(missing);idle=self.auto_service();before=self.config.read_bytes()
        with self.assertRaises((ValueError,OSError)):idle.tick()
        self.assertFalse(self.watch.state()['enabled']);missing.mkdir();idle.tick()
        self.assertEqual(Path(self.watch.state()['folder']),missing);self.assertEqual(self.config.read_bytes(),before)

    def test_new_computer_creates_valid_profile_without_existing_user_config(self):
        self.service.tick();values=read_settings(self.config)
        self.assertEqual(values['LinkedApp'],str(self.app));self.assertEqual(values['IsLinkedAppOther'],'True')
        self.assertEqual(values['NotifyCR3'],'True');self.assertEqual(values['NotifyJPG'],'True')
        root=ET.parse(self.config).getroot();self.assertIsNone(root.find('./configSections'))
        self.assertTrue(self.service.state()['registered']);self.assertFalse(self.watch.state()['enabled'])

    @unittest.skipUnless(os.name=='nt','Windows .NET 配置读取')
    def test_framework_can_read_the_new_profile(self):
        installed=discover_installation()
        if not installed:self.skipTest('未安装 EOS Utility，无法使用实际配置声明')
        self.install['defaults']=installed['defaults']
        self.service.tick();script=self.base/'read-profile.ps1'
        script.write_text("param($Defaults,$Profile,$Roaming)\n$ErrorActionPreference='Stop'\nAdd-Type -AssemblyName System.Configuration\n$m=New-Object System.Configuration.ExeConfigurationFileMap\n$m.ExeConfigFilename=$Defaults\n$m.LocalUserConfigFilename=$Profile\n$m.RoamingUserConfigFilename=$Roaming\n$c=[System.Configuration.ConfigurationManager]::OpenMappedExeConfiguration($m,[System.Configuration.ConfigurationUserLevel]::PerUserRoamingAndLocal)\n$s=$c.GetSection('userSettings/EOSUtility.AppSettings')\n[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n$s.Settings.Get('LinkedApp').Value.ValueXml.InnerText\n",encoding='utf-8-sig')
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),str(self.install['defaults']),str(self.config),str(self.base/'roaming.config')],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode('utf-8-sig').strip(),str(self.app))

    def test_registration_is_idempotent_and_preserves_other_preferences(self):
        self.existing_config();self.service.tick();before=self.config.stat().st_mtime_ns
        self.service.tick();self.assertEqual(self.config.stat().st_mtime_ns,before)
        self.assertEqual(read_settings(self.config)['Unrelated'],'原设置');self.assertEqual(read_settings(self.config)['SaveFolder'],str(self.photos))
        self.assertEqual(len(list(self.service.folder.glob('*.original.config'))),1)

    def test_running_eos_is_not_modified_and_save_folder_is_attached_automatically(self):
        self.existing_config();before=self.config.read_bytes();self.running=True;self.service.tick()
        self.assertEqual(self.config.read_bytes(),before);self.assertTrue(self.service.state()['pending'])
        self.assertEqual(Path(self.watch.state()['folder']),self.photos);self.assertEqual(self.watch.state()['source'],'canon')
        self.running=False;self.service.tick();self.assertTrue(self.service.state()['registered'])

    def test_running_before_first_launch_waits_without_fabricating_profile(self):
        self.running=True;self.service.tick();self.assertFalse(self.config.exists());self.assertTrue(self.service.state()['pending'])

    def test_notifications_preserve_generation_in_same_folder_and_switch_directory(self):
        first=self.picture();one=self.service.receive([str(first)]);generation=self.watch.state()['generation']
        two=self.service.receive([str(self.picture(name='第二张.JPG'))])
        self.assertEqual(one['session_id'],two['session_id']);self.assertEqual(generation,self.watch.state()['generation'])
        other=self.picture(self.base/'日期子目录');three=self.service.receive([str(other)])
        self.assertNotEqual(three['session_id'],one['session_id']);self.assertNotEqual(generation,self.watch.state()['generation'])
        self.assertEqual(self.service.state()['last_file'],str(other))

    def test_actual_date_folder_is_not_overridden_by_fallback_base(self):
        self.existing_config();self.running=True;photo=self.picture(self.photos/'2026-10-22')
        self.service.receive([str(photo)]);self.service.tick()
        self.assertEqual(Path(self.watch.state()['folder']),photo.parent)

    def test_disabled_and_manual_pause_do_not_reopen_monitor(self):
        self.existing_config();self.running=True;photo=self.picture();self.service.tick()
        self.service.pause();self.watch.stop();self.service.tick()
        self.assertFalse(self.watch.state()['enabled']);self.assertFalse(self.service.receive([str(photo)])['accepted'])
        self.service.configure(True);self.service.tick();self.assertTrue(self.watch.state()['enabled'])
        self.service.configure(False);self.service.tick();self.assertFalse(self.watch.state()['enabled'])

    def test_restore_only_our_fields_preserves_later_eos_preferences(self):
        self.existing_config();before=read_settings(self.config);self.service.tick()
        root=ET.parse(self.config);root.find('.//setting[@name="Unrelated"]/value').text='EOS 后来修改';root.write(self.config,encoding='utf-8')
        self.service.configure(False);self.service.tick();restored=read_settings(self.config)
        self.assertEqual(restored['LinkedApp'],before['LinkedApp']);self.assertEqual(restored['IsLinkedAppDPP'],'True')
        self.assertEqual(restored['Unrelated'],'EOS 后来修改')

    def test_restore_does_not_overwrite_user_new_link(self):
        self.existing_config();self.service.tick();root=ET.parse(self.config)
        root.find('.//setting[@name="LinkedApp"]/value').text='D:/用户选择.exe';root.write(self.config,encoding='utf-8')
        self.service.configure(False);self.service.tick();self.assertEqual(read_settings(self.config)['LinkedApp'],'D:/用户选择.exe')

    def test_move_installation_updates_path_and_keeps_original_backup(self):
        self.existing_config();self.service.tick();moved=self.base/'新安装/末帧选片.exe';moved.parent.mkdir();moved.write_bytes(b'MZ')
        self.service.application=moved;self.service.tick();self.assertEqual(read_settings(self.config)['LinkedApp'],str(moved))
        self.service.configure(False);self.service.tick();self.assertEqual(read_settings(self.config)['LinkedApp'],'')

    def test_invalid_notifications_cannot_change_current_session(self):
        for paths in (None,[],['relative.JPG'],[str(self.photos)],[str(self.base/'missing.JPG')]):
            with self.assertRaises((ValueError,OSError)):self.service.receive(paths)
        self.assertIsNone(self.store.session())

    def test_destination_sets_real_eos_original_names_and_date_rules_and_restores(self):
        self.existing_config();original=read_settings(self.config);self.service.tick()  # 已登记链接后再接管保存设置。
        target=self.base/'新保存目录';target.mkdir()
        state=self.service.configure_destination(str(target))
        self.assertTrue(state['destination_ready']);self.assertFalse(state['destination_pending']);self.assertTrue(state['keep_filename'])
        settings=read_settings(self.config)
        self.assertEqual(settings['SaveFolder'],str(target));self.assertEqual(settings['FileCustomizeIndex'],'0')
        self.assertEqual(settings['SubFolderDownload'],'True');self.assertEqual(settings['FolderNameSeparator'],'1')
        self.assertEqual(settings['FolderDateOrder'],'0');self.assertEqual(settings['FolderDateFormat'],'0');self.assertEqual(settings['Unrelated'],'原设置')
        self.assertEqual(Path(self.watch.state()['folder']),target);self.assertEqual(self.watch.state()['source'],'canon')
        reopened=CanonLink(self.store,self.watch,self.app,discover=lambda:self.install,autostart=False)
        try:self.assertEqual(reopened.state()['destination'],str(target));reopened.configure(True);self.assertEqual(json.loads(reopened.preferences.read_text(encoding='utf8'))['destination'],str(target))
        finally:reopened.close()
        self.service.configure(False);self.service.tick();restored=read_settings(self.config)
        for key in ['SaveFolder',*SAVE_VALUES]:self.assertEqual(restored[key],original[key],key)

    def test_running_eos_stages_destination_without_changing_live_profile_or_monitor(self):
        self.existing_config();self.running=True;self.service.tick();before=self.config.read_bytes();old=self.watch.state()['folder']
        target=self.base/'新拍摄根目录';target.mkdir();state=self.service.configure_destination(str(target))
        self.assertTrue(state['destination_pending']);self.assertFalse(state['destination_ready']);self.assertIn('关闭 EOS',state['message'])
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.watch.state()['folder'],old)
        self.running=False;self.service.tick();self.assertTrue(self.service.state()['destination_ready'])
        self.assertEqual(read_settings(self.config)['SaveFolder'],str(target))

    def test_invalid_or_unwritable_destination_preserves_previous_configuration(self):
        self.existing_config();before=self.config.read_bytes()
        for value in ['',None,'relative',r'192.168.1.10\share',str(self.base/'missing'),str(self.app)]:
            with self.assertRaises((ValueError,OSError)):self.service.configure_destination(value)
        with patch('canon_link.tempfile.TemporaryFile',side_effect=PermissionError('只读共享')):
            with self.assertRaisesRegex(ValueError,'无法写入'):self.service.configure_destination(str(self.photos))
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.service.state()['destination'],'')

    @unittest.skipUnless(os.name=='nt','Windows 网络路径')
    def test_unc_and_mapped_drive_are_preserved_when_staging_with_mocked_storage(self):
        self.existing_config();self.running=True
        for value in [r'\\192.168.1.10\摄影共享\拍摄',r'Z:\现场拍摄',r'\\NAS\照片']:
            with patch.object(Path,'resolve',lambda path,*args,**kwargs:path),patch.object(Path,'is_dir',return_value=True),patch('canon_link.tempfile.TemporaryFile') as probe:
                state=self.service.configure_destination(value)
            self.assertEqual(state['destination'],str(Path(value)));self.assertTrue(state['destination_pending']);self.assertEqual(str(probe.call_args.kwargs['dir']),str(Path(value)))

    def test_destination_does_not_override_actual_date_folder_after_notification(self):
        from datetime import date
        self.service.configure_destination(str(self.photos));photo=self.picture(self.photos/date.today().isoformat(),'IMG_1842.JPG')
        self.service.receive([str(photo)]);self.service.tick()
        self.assertEqual(Path(self.watch.state()['folder']),photo.parent);self.assertTrue(photo.is_file())
        self.assertEqual(list(photo.parent.iterdir()),[photo])

    def test_internal_data_destination_is_rejected_before_config_or_probe(self):
        self.existing_config();before=self.config.read_bytes();old=self.service.preferences.read_bytes()
        with patch('canon_link.tempfile.TemporaryFile') as probe:
            with self.assertRaisesRegex(ValueError,'缓存目录'):self.service.configure_destination(str(self.store.data))
            probe.assert_not_called()
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.service.preferences.read_bytes(),old)

    def test_existing_photos_and_sidecars_unchanged_through_entire_link_lifecycle(self):
        from test_monitor_source_safety import snapshot
        self.existing_config();photo=self.picture();self.picture(self.photos/'日期子目录')
        photo.with_suffix('.xmp').write_bytes(b'existing sidecar');(self.photos/'notes.txt').write_bytes(b'user notes')
        before=snapshot(self.photos)
        for _ in range(2):
            self.service.configure_destination(str(self.photos));self.service.receive([str(photo)])
            self.service.pause();self.watch.stop();self.service.configure(True);self.service.tick()
            self.service.configure(False);self.service.tick()
            self.assertEqual(snapshot(self.photos),before)

    def test_unknown_schema_and_malformed_xml_are_not_overwritten(self):
        self.config.parent.mkdir(parents=True);self.config.write_bytes(b'<broken');before=self.config.read_bytes()
        with self.assertRaises(ET.ParseError):self.service.tick()
        self.assertEqual(self.config.read_bytes(),before)

    def test_authenticated_desktop_route_accepts_only_owner_token(self):
        import server
        with ExitStack() as stack:
            stack.enter_context(patch.object(server,'CANON',self.service));stack.enter_context(patch.dict(os.environ,FRAMEKEEP_DESKTOP_TOKEN='test-desktop'))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                body=json.dumps({'paths':[str(self.picture())]}).encode()
                def request(token):return urllib.request.Request(f'http://127.0.0.1:{http.server_port}/api/desktop/canon',data=body,headers={'X-Framekeep-Desktop':token,'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as rejected:urllib.request.urlopen(request('wrong'))
                self.assertEqual(rejected.exception.code,403)
                with urllib.request.urlopen(request('test-desktop')) as response:self.assertTrue(json.load(response)['accepted'])
            finally:http.shutdown();http.server_close()


class InstalledCanonProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows 元数据探测')
    def test_installed_identity_matches_existing_profile_without_execution(self):
        installation=discover_installation()
        if not installation:self.skipTest('未安装 EOS Utility')
        self.assertTrue(Path(installation['defaults']).is_file())
        self.assertIn('IsLinkedAppOther',read_settings(installation['defaults']))
        self.assertEqual(Path(installation['config']).name,'user.config')


if __name__=='__main__':unittest.main()
