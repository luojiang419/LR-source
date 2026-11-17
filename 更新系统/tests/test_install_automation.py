"""默认 D 盘、已有/残缺安装识别、最新版选择和正常退出绑定。"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import installation as i
import updater as u


class InstallAutomationTests(unittest.TestCase):
    def test_default_d_and_permission_request_keep_same_target(self):
        drives=[{'root':'D:\\','free':10*i.MIN_FREE}]
        plan=i.choose_directory(drives,can_write=lambda _:False)
        self.assertEqual(plan['root'],r'D:\Program Files\Framekeep');self.assertTrue(plan['requiresElevation'])

    def test_partial_install_keeps_directory_and_requires_permission_instead_of_relocating(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'old';root.mkdir();i.write_json(root/i.MARKER,{'product':i.PRODUCT,'root':str(root)})
            plan=i.choose_directory([],registered=str(root),can_write=lambda _:False)
            self.assertTrue(plan['existing']);self.assertTrue(plan['requiresElevation']);self.assertEqual(plan['root'],str(root))
            i.write_json(root/i.MARKER,{'product':i.PRODUCT,'root':str(root/'elsewhere')});self.assertFalse(i.installation_present(root))

    def test_registry_alternative_and_known_path_are_detected(self):
        backend=Mock();backend.preferences.return_value={};backend.install_locations.return_value=['X:/old']
        with patch.object(i,'integration_backend',return_value=backend),patch.object(i,'installation_present',side_effect=lambda p:str(p)=='X:/old'),patch.object(i,'choose_directory',return_value={'root':'X:/old'}) as choose:
            self.assertEqual(i.install_plan()['root'],'X:/old');choose.assert_called_once_with(registered='X:/old')
        backend.install_locations.return_value=[]
        with patch.object(i,'integration_backend',return_value=backend),patch.object(i,'installation_present',side_effect=lambda p:str(p)=='D:/Program Files/Framekeep'),patch.object(i,'choose_directory',return_value={'root':'D:/Program Files/Framekeep'}) as choose:
            self.assertEqual(i.install_plan()['root'],'D:/Program Files/Framekeep')

    def test_latest_signed_release_replaces_local_package_but_never_downgrades(self):
        with tempfile.TemporaryDirectory() as folder:
            local=Path(folder)/'envelope.json';u.write_json(local,{'embedded':True})
            embedded={'version':'1.0.0','releaseId':'20261109010000'}
            for release,newer in [('20261108000000',False),('20261109010000',False),('20261109020000',True)]:
                settings={'localEnvelope':str(local),'localPackage':'keep.zip','preferLatest':True}
                remote={**embedded,'releaseId':release}
                with patch.object(u,'verify_envelope',return_value=embedded),patch.object(u,'fetch_envelope',return_value=({'remote':True},remote)):
                    envelope,selected=u.select_install_release(settings)
                self.assertEqual(selected,remote if newer else embedded);self.assertEqual('localPackage' in settings,not newer)
            with patch.object(u,'verify_envelope',return_value=embedded),patch.object(u,'fetch_envelope',side_effect=OSError('offline')):
                settings={'localEnvelope':str(local),'localPackage':'keep.zip','preferLatest':True}
                self.assertEqual(u.select_install_release(settings)[1],embedded);self.assertEqual(settings['localPackage'],'keep.zip')

    def test_invalid_embedded_signature_is_never_used(self):
        with tempfile.TemporaryDirectory() as folder:
            local=Path(folder)/'envelope.json';u.write_json(local,{})
            with patch.object(u,'verify_envelope',side_effect=ValueError('signature')),patch.object(u,'fetch_envelope') as fetch,self.assertRaisesRegex(ValueError,'signature'):
                u.select_install_release({'localEnvelope':str(local),'preferLatest':True})
            fetch.assert_not_called()

    def test_active_instance_requires_process_and_http_identity(self):
        row={'enginePid':123,'parentPid':456,'port':8769}
        response=lambda pid:io.BytesIO(json.dumps({'application':'framekeep','process_id':pid}).encode())
        with patch.object(i.WindowsIntegration,'shell',return_value=json.dumps([row])),patch.object(i.urllib.request,'urlopen',return_value=response(123)):
            self.assertEqual(i.active_installation(Path('D:/Program Files/Framekeep')),row)
        with patch.object(i.WindowsIntegration,'shell',return_value=json.dumps([row])),patch.object(i.urllib.request,'urlopen',return_value=response(999)),self.assertRaisesRegex(RuntimeError,'身份校验'):
            i.active_installation(Path('D:/Program Files/Framekeep'))
        with patch.object(i.WindowsIntegration,'shell',return_value=json.dumps([{**row,'parentPid':0}])),self.assertRaisesRegex(RuntimeError,'独立或多个'):
            i.active_installation(Path('D:/Program Files/Framekeep'))

    def test_path_verified_unresponsive_or_orphan_engine_can_be_force_closed(self):
        row={'enginePid':123,'parentPid':0,'port':8769}
        with patch.object(i.WindowsIntegration,'shell',return_value=json.dumps([row])),patch.object(i.urllib.request,'urlopen',side_effect=TimeoutError('hung')):
            self.assertEqual(i.active_installation(Path('D:/app'),allow_unresponsive=True),row)
        response=io.BytesIO(json.dumps({'application':'other','process_id':999}).encode())
        with patch.object(i.WindowsIntegration,'shell',return_value=json.dumps([row])),patch.object(i.urllib.request,'urlopen',return_value=response),self.assertRaisesRegex(RuntimeError,'身份校验'):
            i.active_installation(Path('D:/app'),allow_unresponsive=True)

    def test_completed_embedded_payload_is_cleaned_but_external_seed_is_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);session=root/'session';session.mkdir();external=root/'external';external.mkdir()
            u.write_json(session/'status.json',{'status':'complete'});u.write_json(session/'journal.json',{'status':'complete'})
            for name in ('package.zip','SHIYING-Windows-x64.zip'):(session/name).write_bytes(b'payload')
            (session/'components').mkdir();(session/'components/model').write_bytes(b'model')
            setup={'embeddedPayload':True,'componentSeed':str(external),'localEnvelope':str(session/'envelope.json')};u.write_json(session/'session.json',setup)
            u.release_completed_payload(session);self.assertTrue((session/'components/model').exists());self.assertTrue((session/'SHIYING-Windows-x64.zip').exists())
            setup['componentSeed']=str(session/'components');u.write_json(session/'session.json',setup)
            u.release_completed_payload(session);self.assertFalse((session/'components').exists());self.assertFalse((session/'SHIYING-Windows-x64.zip').exists());self.assertTrue(external.exists())


if __name__=='__main__':unittest.main()
