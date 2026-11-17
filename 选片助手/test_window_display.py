"""F11原生窗口桥接：输入框/弹窗、防连发、错误恢复和快捷键隔离。"""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


class WindowDisplayTests(unittest.TestCase):
    def test_f11_in_input_and_modal_serializes_native_toggle_and_recovers(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.set_content('<input id="editor"><dialog id="modal"><input id="modal-input"></dialog>')
                page.evaluate('''()=>{window.calls=[];window.notices=[];window.Framekeep={notify:m=>notices.push(m)};
                    window.__TAURI__={core:{invoke:(name)=>{calls.push(name);return new Promise((resolve,reject)=>{window.resolveToggle=resolve;window.rejectToggle=reject})}}};
                    document.getElementById('modal').addEventListener('keydown',e=>e.stopPropagation());}''')
                page.add_script_tag(path=str(ROOT/'演示方案/desktop.js'))
                page.locator('#editor').focus();page.keyboard.press('F11')
                self.assertEqual(page.evaluate('()=>calls'),['toggle_fullscreen'])
                page.evaluate("()=>window.dispatchEvent(new KeyboardEvent('keydown',{key:'F11',repeat:true,cancelable:true}))")
                page.keyboard.press('F11')
                self.assertEqual(page.evaluate('()=>calls.length'),1)
                page.evaluate('()=>resolveToggle(true)')
                page.evaluate("()=>document.getElementById('modal').showModal()")
                page.locator('#modal-input').focus();page.keyboard.press('F11')
                self.assertEqual(page.evaluate('()=>calls.length'),2)
                page.evaluate("()=>rejectToggle(new Error('test failure'))")
                page.wait_for_function('()=>notices.length===1')
                page.keyboard.press('F11')
                self.assertEqual(page.evaluate('()=>calls.length'),3)
                page.evaluate('()=>resolveToggle(false)')
                self.assertTrue(page.evaluate("()=>window.dispatchEvent(new KeyboardEvent('keydown',{key:'F11',ctrlKey:true,altKey:true,cancelable:true}))"))
                self.assertEqual(page.evaluate('()=>calls.length'),3)
                self.assertFalse(errors,errors)
                normal=browser.new_page();normal.set_content('<p>browser</p>')
                normal.add_script_tag(path=str(ROOT/'演示方案/desktop.js'))
                self.assertTrue(normal.evaluate("()=>window.dispatchEvent(new KeyboardEvent('keydown',{key:'F11',cancelable:true}))"))
            finally:browser.close()


if __name__=='__main__':unittest.main()
