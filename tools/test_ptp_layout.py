"""Portable layout tests: synthetic descriptors only, plus an opt-in real pad.

These tests never touch the mouse, keyboard or input queue.  The real-device
check opens the touchpad for queries and requires HID read access; it is
skipped automatically when the pad is absent or already held by the tray.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import os
import pad_info
import ptp_layout as L

D, DESK, BTN = L.PAGE_DIGITIZER, L.PAGE_DESKTOP, L.PAGE_BUTTON


def cap(page, usage, data, count=1, logical=(0, 1), link=0, report=3,
        usage_max=None):
    """One synthetic capability; ranges mirror what HidP_GetValueCaps returns."""
    is_range = usage_max is not None
    return L.Capability(
        page=page, usage_min=usage,
        usage_max=usage if usage_max is None else usage_max,
        report_id=report, count=count, data_min=data,
        data_max=data + count - 1 if count > 1 else data,
        logical_min=logical[0], logical_max=logical[1], link=link,
        is_range=is_range)


def link_pad(report=3, contacts=2, x_range=(0, 1332), y_range=(0, 828)):
    """Synaptics-style: one link collection per contact."""
    values, buttons = [], []
    for slot in range(contacts):
        link = slot + 1
        base = slot * 5
        buttons.append(cap(D, L.USAGE_TIP_SWITCH, base, link=link, report=report))
        buttons.append(cap(D, L.USAGE_CONFIDENCE, base + 1, link=link,
                           report=report))
        values.append(cap(D, L.USAGE_CONTACT_ID, base + 2, logical=(0, 15),
                          link=link, report=report))
        values.append(cap(DESK, L.USAGE_X, base + 3, logical=x_range,
                          link=link, report=report))
        values.append(cap(DESK, L.USAGE_Y, base + 4, logical=y_range,
                          link=link, report=report))
    values.append(cap(D, L.USAGE_SCAN_TIME, 25, logical=(0, 65535),
                      report=report))
    values.append(cap(D, L.USAGE_CONTACT_COUNT, 26, logical=(0, contacts),
                      report=report))
    buttons.append(cap(BTN, L.USAGE_BUTTON_1, 27, report=report))
    return values, buttons


class LinkCollectionTests(unittest.TestCase):
    def setUp(self):
        values, buttons = link_pad()
        self.layout = L.build_layout(30, values, buttons)

    def test_builds_one_contact_map_per_link(self):
        self.assertIsNotNone(self.layout)
        report = self.layout.primary
        self.assertEqual(report.max_contacts, 2)
        self.assertEqual(self.layout.input_length, 30)
        self.assertTrue(self.layout.uses_report_ids)
        self.assertEqual(self.layout.report_ids, (3,))
        self.assertEqual(report.count_index, 26)
        self.assertEqual(report.scan_index, 25)
        self.assertEqual(report.button_index, 27)
        self.assertEqual((report.x_min, report.x_max), (0, 1332))
        self.assertEqual((report.y_min, report.y_max), (0, 828))
        self.assertEqual(self.layout.width, 1332)
        self.assertEqual(self.layout.height, 828)

    def test_parse_two_contacts(self):
        values = {26: 2, 25: 1234, 27: 1,
                  0: 1, 1: 1, 2: 0, 3: 948, 4: 588,
                  5: 1, 6: 1, 7: 1, 8: 120, 9: 92}
        frame = self.layout.primary.parse(values, b'\x03' + bytes(29))
        self.assertEqual(frame['count'], 2)
        self.assertEqual(frame['scan'], 1234)
        self.assertEqual(frame['buttons'], 1)
        first, second = frame['contacts']
        self.assertEqual((first['slot'], first['id'], first['x'],
                          first['y'], first['tip'], first['confidence']),
                         (0, 0, 948, 588, True, True))
        self.assertEqual((second['slot'], second['id'], second['x'],
                          second['y']), (1, 1, 120, 92))
        self.assertEqual(first['flags'], 3)
        self.assertEqual(second['flags'], (1 << 2) | 3)
        self.assertEqual(frame['schema_version'], 2)

    def test_tip_switch_without_confidence_is_still_a_contact(self):
        values = [
            cap(D, L.USAGE_CONTACT_ID, 0, logical=(0, 15), link=1),
            cap(DESK, L.USAGE_X, 1, logical=(0, 500), link=1),
            cap(DESK, L.USAGE_Y, 2, logical=(0, 400), link=1),
            cap(D, L.USAGE_TIP_SWITCH, 3, link=1),
        ]
        layout = L.build_layout(4, values, [])
        frame = layout.primary.parse({0: 0, 1: 10, 2: 20, 3: 1}, b'')
        self.assertTrue(frame['contacts'][0]['tip'])
        self.assertTrue(frame['contacts'][0]['confidence'])

    def test_count_is_clamped_to_layout(self):
        values = {26: 9, 0: 1, 2: 0, 3: 1, 4: 2}
        frame = self.layout.primary.parse(values, b'')
        self.assertEqual(frame['count'], 2)
        values[26] = 0
        self.assertEqual(self.layout.primary.parse(values, b'')['count'], 0)


class ParallelArrayTests(unittest.TestCase):
    def setUp(self):
        values = [
            cap(DESK, L.USAGE_X, 0, count=5, logical=(0, 2000), report=0),
            cap(DESK, L.USAGE_Y, 5, count=5, logical=(0, 1000), report=0),
            cap(D, L.USAGE_TIP_SWITCH, 10, count=5, report=0),
            cap(D, L.USAGE_CONFIDENCE, 15, count=5, report=0),
            cap(D, L.USAGE_CONTACT_ID, 20, count=5, logical=(0, 15), report=0),
            cap(D, L.USAGE_CONTACT_COUNT, 25, logical=(0, 5), report=0),
        ]
        buttons = []
        self.layout = L.build_layout(27, values, buttons)

    def test_parallel_arrays_map_slot_by_position(self):
        self.assertEqual(self.layout.primary.max_contacts, 5)
        values = {25: 3,
                  0: 100, 1: 200, 2: 300,
                  5: 10, 6: 20, 7: 30,
                  10: 1, 11: 1, 12: 0,
                  15: 1, 16: 0, 17: 1,
                  20: 4, 21: 5, 22: 6}
        frame = self.layout.primary.parse(values, b'')
        self.assertEqual([c['x'] for c in frame['contacts']], [100, 200, 300])
        self.assertEqual([c['y'] for c in frame['contacts']], [10, 20, 30])
        self.assertEqual([c['tip'] for c in frame['contacts']], [True, True, False])
        self.assertEqual([c['id'] for c in frame['contacts']], [4, 5, 6])

    def test_report_without_report_id(self):
        self.assertFalse(self.layout.uses_report_ids)
        self.assertEqual(self.layout.report_ids, (0,))


class HybridTests(unittest.TestCase):
    def test_single_contact_without_contact_count(self):
        values = [
            cap(D, L.USAGE_CONTACT_ID, 0, logical=(0, 15), link=1),
            cap(DESK, L.USAGE_X, 1, logical=(0, 500), link=1),
            cap(DESK, L.USAGE_Y, 2, logical=(0, 400), link=1),
            cap(D, L.USAGE_TIP_SWITCH, 3, link=1),
        ]
        layout = L.build_layout(4, values, [])
        self.assertIsNotNone(layout)
        self.assertIsNone(layout.primary.count_index)
        frame = layout.primary.parse({0: 2, 1: 250, 2: 100, 3: 1}, b'')
        self.assertEqual(frame['count'], 1)
        self.assertEqual((frame['contacts'][0]['id'],
                          frame['contacts'][0]['x'],
                          frame['contacts'][0]['y']),
                         (2, 250, 100))


class NormalisationTests(unittest.TestCase):
    def test_nonzero_logical_minimum_is_subtracted(self):
        values = [
            cap(DESK, L.USAGE_X, 0, logical=(100, 1100), link=1),
            cap(DESK, L.USAGE_Y, 1, logical=(200, 1200), link=1),
        ]
        layout = L.build_layout(2, values, [])
        self.assertEqual(layout.width, 1000)
        self.assertEqual(layout.height, 1000)
        low = layout.primary.parse({0: 100, 1: 200}, b'')
        high = layout.primary.parse({0: 1100, 1: 1200}, b'')
        self.assertEqual((low['contacts'][0]['x'], low['contacts'][0]['y']),
                         (0, 0))
        self.assertEqual((high['contacts'][0]['x'], high['contacts'][0]['y']),
                         (1000, 1000))

    def test_inverted_or_empty_range_is_rejected(self):
        values = [cap(DESK, L.USAGE_X, 0, logical=(10, 10), link=1),
                  cap(DESK, L.USAGE_Y, 1, logical=(0, 10), link=1)]
        self.assertIsNone(L.build_layout(2, values, []))

    def test_collection_without_x_or_y_is_not_a_layout(self):
        values = [cap(D, L.USAGE_CONTACT_COUNT, 0, logical=(0, 5))]
        self.assertIsNone(L.build_layout(3, values, []))
        self.assertIsNone(L.build_layout(0, values, []))


class MultipleReportsTests(unittest.TestCase):
    def test_report_switch_prefers_richest_layout(self):
        values2, buttons2 = link_pad(report=2, contacts=1)
        values3, buttons3 = link_pad(report=3, contacts=4)
        layout = L.build_layout(30, values2 + values3, buttons2 + buttons3)
        self.assertEqual(layout.report_ids, (2, 3))
        self.assertEqual(layout.primary.report_id, 3)
        self.assertEqual(layout.max_contacts, 4)

    def test_occurrences_expand_a_range(self):
        item = cap(D, L.USAGE_TIP_SWITCH, 0, count=3,
                   usage_max=L.USAGE_TIP_SWITCH + 2)
        self.assertTrue(item.is_range)
        self.assertEqual([(u, i) for u, i, _, _ in item.occurrences()],
                         [(0x42, 0), (0x43, 1), (0x44, 2)])


class DiscoveryTests(unittest.TestCase):
    def test_override_matches_vid_pid_or_path(self):
        device = L.PadDevice(path=r'\\?\HID#SYN1B8B&Col02#7', layout=None,
                             vendor_id=0x06CB, product_id=0xCE17)
        self.assertTrue(L._matches_override(device, ''))
        self.assertTrue(L._matches_override(device, '06cb:ce17'))
        self.assertTrue(L._matches_override(device, 'col02'))
        self.assertFalse(L._matches_override(device, '04f3:1234'))

    def test_discover_dedupes_and_filters(self):
        first = L.PadDevice(path='p1', layout=None, vendor_id=1, product_id=2)
        second = L.PadDevice(path='p2', layout=None, vendor_id=3, product_id=4)
        entries = [
            {'path': b'p1', 'usage_page': 13, 'usage': 5},
            {'path': b'p1', 'usage_page': 13, 'usage': 5},
            {'path': b'p2', 'usage_page': 13, 'usage': 5},
            {'path': b'p3', 'usage_page': 1, 'usage': 2},
        ]
        with patch.object(L.hid, 'enumerate', return_value=entries), \
                patch.object(L, 'load_device', side_effect=[first, second]):
            device, candidates, errors = L.discover()
        self.assertIs(device, first)
        self.assertEqual([d.path for d in candidates], ['p1', 'p2'])
        self.assertEqual(errors, [])
        with patch.object(L.hid, 'enumerate', return_value=entries), \
                patch.object(L, 'load_device', side_effect=[first, second]):
            device, candidates, errors = L.discover('0003:0004')
        self.assertIs(device, second)
        self.assertEqual(candidates, [second])

    def test_discover_reports_bad_collections_and_continues(self):
        good = L.PadDevice(path='ok', layout=None)
        entries = [{'path': b'bad', 'product_string': 'Bad',
                    'usage_page': 13, 'usage': 5},
                   {'path': b'ok', 'product_string': 'OK',
                    'usage_page': 13, 'usage': 5}]
        with patch.object(L.hid, 'enumerate', return_value=entries), \
                patch.object(L, 'load_device',
                             side_effect=[L.LayoutError('没有触点'), good]):
            device, candidates, errors = L.discover()
        self.assertIs(device, good)
        self.assertEqual(candidates, [good])
        self.assertEqual(errors, ['Bad: 没有触点'])

    def test_discover_survives_enumeration_failure(self):
        with patch.object(L.hid, 'enumerate', side_effect=OSError('gone')):
            device, candidates, errors = L.discover()
        self.assertIsNone(device)
        self.assertEqual(candidates, [])
        self.assertEqual(len(errors), 1)


class PadInfoTests(unittest.TestCase):
    def setUp(self):
        pad_info.reset()
        self.addCleanup(pad_info.reset)

    def test_defaults_before_discovery(self):
        self.assertIsNone(pad_info.device())
        self.assertEqual((pad_info.max_x(), pad_info.max_y()), (1332, 828))
        self.assertEqual(pad_info.max_contacts(), 5)
        self.assertAlmostEqual(pad_info.edge_x(), 110.0)
        self.assertAlmostEqual(pad_info.edge_y(), 60.0)

    def test_configure_caches_the_discovered_pad(self):
        fake = SimpleNamespace(max_x=2000, max_y=1000, max_contacts=10,
                               describe=lambda: 'fake 2000x1000')
        with patch.object(L, 'discover', return_value=(fake, [fake], [])) as fn:
            self.assertIs(pad_info.configure(), fake)
            self.assertIs(pad_info.configure(), fake)
        self.assertEqual(fn.call_count, 1)
        self.assertEqual((pad_info.max_x(), pad_info.max_y()), (2000, 1000))
        self.assertEqual(pad_info.max_contacts(), 10)
        self.assertAlmostEqual(pad_info.edge_x(), 2000 * 110.0 / 1332)

    def test_failure_message_includes_errors(self):
        with patch.object(L, 'discover', return_value=(None, [], ['a: b'])):
            self.assertIsNone(pad_info.configure())
        self.assertIn('a: b', pad_info.failure())
        self.assertIn('0x0D/0x05', pad_info.failure())

    def test_environment_can_choose_the_device(self):
        fake = SimpleNamespace(max_x=1, max_y=1, max_contacts=1,
                               path='p1', vendor_id=1, product_id=2,
                               describe=lambda: 'fake')
        with patch.dict(os.environ, {'TOUCHPAD_INK_DEVICE': '0001:0002'}), \
                patch.object(L, 'discover', return_value=(fake, [fake], [])) as fn:
            self.assertIs(pad_info.configure(), fake)
            self.assertIs(pad_info.configure(), fake)
        fn.assert_called_once_with('0001:0002')


class RealPadTests(unittest.TestCase):
    """Opt-in: decodes one recorded frame when the actual pad is reachable."""

    HEX = '0303b4034c020000000000000000000000000000000000000000860a0100'

    def test_recorded_frame_matches_legacy_decoder(self):
        try:
            device, _, _ = L.discover()
        except BaseException as exc:  # pragma: no cover - environment gated
            self.skipTest('no HID access: %s' % exc)
        if device is None:
            self.skipTest('no precision touchpad in this session')
        try:
            reader = L.HidReader(device)
        except L.LayoutError as exc:
            self.skipTest('pad not readable: %s' % exc)
        try:
            frame = reader.read(bytes.fromhex(self.HEX))
        finally:
            reader.close()
        if frame is None:
            self.skipTest('pad reports a different report size')
        contact = frame['contacts'][0]
        self.assertEqual((contact['x'], contact['y']), (948, 588))
        self.assertEqual((frame['scan'], frame['count']), (2694, 1))


if __name__ == '__main__':
    unittest.main()
