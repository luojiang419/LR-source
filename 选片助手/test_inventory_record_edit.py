"""手机更正款号后的修订同步与旧账本兼容。"""
import sqlite3
from contextlib import closing
import json
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
import unittest
import test_inventory as base
from test_inventory import record
import test_inventory_usb as usb_base
from inventory_service import InventoryService


class InventoryRecordEditTests(unittest.TestCase):
    setUp = base.InventoryTests.setUp
    tearDown = base.InventoryTests.tearDown

    def test_edit_updates_same_record_with_images_and_timestamp_unchanged(self):
        original = record(); self.s.receive(original)
        edited = {**original, 'sku': 'LC-CORRECTED', 'source': 'manual', 'revision': 1}
        self.assertFalse(self.s.has_complete(edited))
        ack = self.s.receive(edited)
        self.assertTrue(ack['updated']); self.assertEqual(ack['revision'], 1)
        rows = self.s.records({})['rows']; self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sku'], 'LC-CORRECTED')
        for key in ['id', 'at', 'mode', 'raw', 'product_sha', 'label_sha']:
            self.assertEqual(rows[0][key], original[key])
        self.assertTrue(self.s.has_complete(edited))
        self.assertTrue(self.s.receive(edited)['duplicate'])
        self.assertEqual(self.s.records({'query': original['sku']})['total'], 0)

    def test_old_upload_cannot_roll_back_newer_edit(self):
        original = record(); self.s.receive(original)
        edited = {**original, 'sku': 'LC-NEW', 'source': 'manual', 'revision': 2}
        self.s.receive(edited)
        self.assertEqual(self.s.receive(original)['revision'], 2)
        self.assertEqual(self.s.records({})['rows'][0]['sku'], 'LC-NEW')
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.s.receive({**edited, 'sku': 'LC-CONFLICT'})

    def test_new_revision_never_overwrites_immutable_identity_or_photos(self):
        original = record(); self.s.receive(original)
        for field, value in [('mode', 'out'), ('at', '2026-11-06T15:00:00+08:00'), ('raw', 'other')]:
            edited = {**original, 'sku': 'LC-EDIT', 'source': 'manual', 'revision': 1, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '冲突'): self.s.receive(edited)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.s.receive({**original, 'sku': 'LC-EDIT', 'revision': 1})
        self.assertEqual(self.s.records({})['rows'][0]['sku'], original['sku'])

    def test_invalid_revision_rejected_and_newer_duplicate_repairs_photo(self):
        original = record(); self.s.receive(original)
        for value in [-1, '1', True, 2**31]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '修订'):
                self.s.receive({**original, 'revision': value})
        path = self.s.images / f'{original["id"]}-label.jpg'; path.unlink()
        self.s.receive({**original, 'sku': 'LC-EDIT', 'source': 'manual', 'revision': 1})
        self.assertTrue(path.is_file())

    def test_legacy_database_gets_revision_zero_without_losing_records(self):
        original = record(); self.s.receive(original); self.s.close()
        with closing(sqlite3.connect(self.s.db)) as db:
            with db: db.execute('ALTER TABLE records DROP COLUMN revision')
        self.s = InventoryService(self.tmp.name, autostart=False)
        row = self.s.records({})['rows'][0]
        self.assertEqual(row['sku'], original['sku']); self.assertEqual(row['revision'], 0)


class InventoryEditedUsbTests(unittest.TestCase):
    setUp = usb_base.InventoryUsbTests.setUp
    tearDown = usb_base.InventoryUsbTests.tearDown

    def test_previously_synced_sku_edit_is_imported_without_duplicate(self):
        self.link.sync()
        original = self.phone.rows[0][1]
        edited = {**original, 'sku': 'LC-EDITED', 'source': 'manual', 'revision': 1}
        self.phone.rows = [(2, edited)]
        sent = []
        request = self.link.request
        def capture(path, data=None):
            if path == '/v1/ack': sent.append(data)
            return request(path, data)
        self.link.request = capture
        self.link.sync()
        self.assertEqual(self.owner.state()['summary']['total'], 1)
        self.assertEqual(self.owner.records({})['rows'][0]['sku'], 'LC-EDITED')
        self.assertEqual(sent[0]['revision'], 1)
        self.assertEqual(self.phone.fetches, 2)

    def test_manual_network_pull_acknowledges_edited_revision(self):
        original = self.phone.rows[0][1]
        self.owner.receive(original)
        edited = {**original, 'sku': 'LC-NETWORK-EDIT', 'source': 'manual', 'revision': 1}
        captured = []
        original_get = usb_base.FakePhone.do_GET
        def get(handler):
            url = urlsplit(handler.path)
            if url.path != '/v1/records': return original_get(handler)
            offset = int(parse_qs(url.query)['offset'][0])
            return handler.answer({'records': [edited] if offset == 0 else [], 'next': 1})
        def post(handler):
            captured.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
            handler.answer({'ok': True})
        with patch.object(usb_base.FakePhone, 'do_GET', get), patch.object(usb_base.FakePhone, 'do_POST', post):
            self.owner.pull({'url': self.link.session['url'], 'token': 'a'*32})
        self.assertEqual(captured[0]['revision'], 1)
        self.assertEqual(self.owner.records({})['rows'][0]['sku'], 'LC-NETWORK-EDIT')


if __name__ == '__main__': unittest.main()
