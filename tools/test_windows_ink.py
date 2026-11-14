import json
from pathlib import Path
import unittest

import windows_ink

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / 'data' / 'handwrite_01.jsonl'


def first_character():
    """First verified 4-stroke '手' from the legacy sample recording."""
    with SAMPLE.open(encoding='utf-8') as handle:
        frames = [json.loads(line) for line in handle]
    active, strokes = {}, []
    for frame in frames:
        if frame['t'] > 4.7:
            break
        seen = set()
        for contact in frame['contacts']:
            cid = contact['id']
            seen.add(cid)
            # Legacy names: 'inrange' holds Tip, 'tip' holds Confidence.
            if not (contact['inrange'] and contact['tip']):
                active.pop(cid, None)
                continue
            point = (contact['x'], contact['y'])
            if cid in active and frame['t'] - active[cid][0] <= 0.15:
                active[cid][1].append(point)
                active[cid][0] = frame['t']
            else:
                active[cid] = [frame['t'], [point]]
                strokes.append(active[cid][1])
        for cid in list(active):
            if cid not in seen:
                del active[cid]
    return strokes


class WindowsInkTests(unittest.TestCase):
    def test_empty_strokes_rejected(self):
        with self.assertRaises(ValueError):
            windows_ink.recognize([])

    @unittest.skipUnless(windows_ink.is_available(), 'Windows Ink is not installed')
    def test_recorded_shou(self):
        strokes = first_character()
        self.assertEqual([len(s) for s in strokes], [35, 48, 58, 168])
        result = windows_ink.recognize(strokes)
        self.assertEqual(result['candidates'][0], '手')
        self.assertIn('手', result['text'])


class WorkerTests(unittest.TestCase):
    @unittest.skipUnless(windows_ink.is_available(), 'Windows Ink is not installed')
    def test_many_recognitions_share_one_worker(self):
        # A fresh InkRecognizerContainer per call segfaulted around call ten;
        # the app now keeps one worker thread and one cached recognizer.
        strokes = first_character()
        try:
            first = windows_ink._worker_instance()
            for _ in range(12):
                result = windows_ink.recognize(strokes)
            self.assertTrue(result['results'])
            self.assertIs(windows_ink._worker_instance(), first)
        finally:
            windows_ink._stop_worker()


if __name__ == '__main__':
    unittest.main()
