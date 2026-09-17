#!/usr/bin/env python3
"""Tests for layout_store — per-session snapshot parsing, pane ordering,
snapshot assembly, restore planning, and the on-disk store. None of these
need tmux."""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import layout_store
from layout_store import Ref

NOW = '2026-09-17T13:20:11+09:00'
HOME = '/home/tester'

# 3 panes: a full-width top pane, and two side by side underneath.
# The layout tree visits them as 0, 1, 2.
LAYOUT3 = '81f0,200x50,0,0[200x25,0,0,0,200x24,0,26{100x24,0,26,1,99x24,101,26,2}]'
LAYOUT1 = '5963,80x24,0,0,7'


def win_line(index, layout, active, width, height, name):
    return f'{index}\t{layout}\t{"1" if active else "0"}\t{width}\t{height}\t{name}'


def pane_line(window, num, pane_index, active, cwd):
    return f'{window}\t%{num}\t{pane_index}\t{"1" if active else "0"}\t{cwd}'


def always_dir(_path):
    return True


def session_snap(session='web', attached=False, windows=None):
    """One entry of a snapshot's `sessions` list, as validate/restore see it."""
    if windows is None:
        windows = [{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                    'panes': [{'cwd': '/a', 'active': True}]}]
    return {
        'session': session, 'attached': attached,
        'width': 200, 'height': 50, 'pane_order': 'layout', 'active_window': 0,
        'windows': windows,
    }


def snapshot(name='slot', sessions=None, saved_at=NOW):
    if sessions is None:
        sessions = [session_snap()]
    return {'name': name, 'saved_at': saved_at, 'sessions': sessions}


class ParseSessionSnapshotTest(unittest.TestCase):

    def test_parses_windows_and_panes(self):
        win = win_line(0, LAYOUT3, True, 200, 50, 'my editor')
        panes = '\n'.join((
            pane_line(0, 0, 0, False, '/home/tester/a'),
            pane_line(0, 1, 1, True,  '/home/tester/b'),
            pane_line(0, 2, 2, False, '/home/tester/c'),
        ))
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=True, isdir=always_dir, home=HOME)

        self.assertEqual(snap['session'], 'web')
        self.assertTrue(snap['attached'])
        self.assertEqual(snap['width'], 200)
        self.assertEqual(snap['height'], 50)
        self.assertEqual(snap['pane_order'], 'layout')
        self.assertEqual(snap['active_window'], 0)
        self.assertEqual(len(snap['windows']), 1)
        self.assertEqual(snap['windows'][0]['name'], 'my editor')
        self.assertEqual([p['cwd'] for p in snap['windows'][0]['panes']],
                         ['/home/tester/a', '/home/tester/b', '/home/tester/c'])
        self.assertTrue(snap['windows'][0]['panes'][1]['active'])

    def test_attached_flag_is_passed_through(self):
        win = win_line(0, LAYOUT1, True, 80, 24, 'w')
        panes = pane_line(0, 7, 0, True, '/a')

        detached = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=always_dir, home=HOME)
        attached = layout_store.parse_session_snapshot(
            'web', win, panes, attached=True, isdir=always_dir, home=HOME)

        self.assertFalse(detached['attached'])
        self.assertTrue(attached['attached'])

    def test_free_text_fields_may_contain_separators(self):
        # window_name and pane_current_path come last in the -F string, so tabs
        # and pipes inside them must survive the split.
        win = win_line(0, LAYOUT1, True, 80, 24, 'name\twith|junk')
        panes = pane_line(0, 7, 0, True, '/home/tester/odd\tdir|名前')
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=always_dir, home=HOME)

        self.assertEqual(snap['windows'][0]['name'], 'name\twith|junk')
        self.assertEqual(snap['windows'][0]['panes'][0]['cwd'], '/home/tester/odd\tdir|名前')

    def test_multiple_windows_keep_their_indices(self):
        wins = '\n'.join((
            win_line(0, LAYOUT1, False, 80, 24, 'one'),
            win_line(3, LAYOUT1, True,  120, 40, 'two'),
        ))
        panes = '\n'.join((
            pane_line(0, 7, 0, True, '/a'),
            pane_line(3, 7, 0, True, '/b'),
        ))
        snap = layout_store.parse_session_snapshot(
            'web', wins, panes, attached=False, isdir=always_dir, home=HOME)

        self.assertEqual([w['index'] for w in snap['windows']], [0, 3])
        self.assertEqual(snap['active_window'], 3)
        # Size comes from the active window, not the first one.
        self.assertEqual((snap['width'], snap['height']), (120, 40))

    def test_panes_follow_layout_tree_order_not_pane_index(self):
        # As if swap-pane had run: pane %2 sits in the first layout cell.
        layout = '81f0,200x50,0,0[200x25,0,0,2,200x24,0,26{100x24,0,26,1,99x24,101,26,0}]'
        win = win_line(0, layout, True, 200, 50, 'w')
        panes = '\n'.join((
            pane_line(0, 0, 0, False, '/pane-index-0'),
            pane_line(0, 1, 1, False, '/pane-index-1'),
            pane_line(0, 2, 2, True,  '/pane-index-2'),
        ))
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=always_dir, home=HOME)

        self.assertEqual(snap['pane_order'], 'layout')
        self.assertEqual([p['cwd'] for p in snap['windows'][0]['panes']],
                         ['/pane-index-2', '/pane-index-1', '/pane-index-0'])

    def test_falls_back_to_index_order_when_layout_does_not_match(self):
        # Layout names panes 0/1/2 but the window reports 0/1/5.
        win = win_line(0, LAYOUT3, True, 200, 50, 'w')
        panes = '\n'.join((
            pane_line(0, 0, 0, True,  '/a'),
            pane_line(0, 1, 1, False, '/b'),
            pane_line(0, 5, 2, False, '/c'),
        ))
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=always_dir, home=HOME)

        self.assertEqual(snap['pane_order'], 'index')
        self.assertEqual([p['cwd'] for p in snap['windows'][0]['panes']], ['/a', '/b', '/c'])

    def test_missing_directory_falls_back_to_home(self):
        win = win_line(0, LAYOUT1, True, 80, 24, 'w')
        panes = pane_line(0, 7, 0, True, '/gone')
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=lambda p: False, home=HOME)

        self.assertEqual(snap['windows'][0]['panes'][0]['cwd'], HOME)

    def test_undecodable_directory_falls_back_to_home(self):
        broken = '/home/tester/' + '\udcff'          # surrogateescape leftover
        win = win_line(0, LAYOUT1, True, 80, 24, 'w')
        panes = pane_line(0, 7, 0, True, broken)
        snap = layout_store.parse_session_snapshot(
            'web', win, panes, attached=False, isdir=always_dir, home=HOME)

        self.assertEqual(snap['windows'][0]['panes'][0]['cwd'], HOME)

    def test_rejects_too_many_panes(self):
        wins, panes, num = [], [], 0
        for w in range(9):                            # 9 x 15 = 135 > MAX_TOTAL_PANES
            wins.append(win_line(w, LAYOUT1, w == 0, 80, 24, f'w{w}'))
            for p in range(15):
                panes.append(pane_line(w, num, p, p == 0, '/a'))
                num += 1
        snap = layout_store.parse_session_snapshot(
            'web', '\n'.join(wins), '\n'.join(panes),
            attached=False, isdir=always_dir, home=HOME)

        self.assertIsNone(snap)

    def test_rejects_empty_or_unreadable_output(self):
        self.assertIsNone(layout_store.parse_session_snapshot(
            'web', '', '', attached=False, isdir=always_dir, home=HOME))
        # A window with no panes at all means the listing was truncated.
        self.assertIsNone(layout_store.parse_session_snapshot(
            'web', win_line(0, LAYOUT1, True, 80, 24, 'w'), '',
            attached=False, isdir=always_dir, home=HOME))

    def test_rejects_bad_session_name(self):
        win = win_line(0, LAYOUT1, True, 80, 24, 'w')
        panes = pane_line(0, 7, 0, True, '/a')
        for session in ('', 'a:b', 'a\nb'):
            with self.subTest(session=session):
                self.assertIsNone(layout_store.parse_session_snapshot(
                    session, win, panes, attached=False, isdir=always_dir, home=HOME))


class BuildSnapshotTest(unittest.TestCase):

    def test_wraps_sessions_under_a_name(self):
        sessions = [session_snap('web'), session_snap('dev')]
        snap = layout_store.build_snapshot('毎朝', sessions, now=NOW)

        self.assertEqual(snap, {'name': '毎朝', 'saved_at': NOW, 'sessions': sessions})

    def test_rejects_bad_name(self):
        for name in ('', '  ', 'x' * 65, 'a\nb'):
            with self.subTest(name=name):
                self.assertIsNone(layout_store.build_snapshot(name, [session_snap()], now=NOW))

    def test_rejects_no_sessions(self):
        self.assertIsNone(layout_store.build_snapshot('slot', [], now=NOW))

    def test_rejects_too_many_sessions(self):
        sessions = [session_snap(f's{i}') for i in range(layout_store.MAX_SESSIONS_PER_SNAPSHOT + 1)]
        self.assertIsNone(layout_store.build_snapshot('slot', sessions, now=NOW))

    def test_accepts_the_session_cap_exactly(self):
        sessions = [session_snap(f's{i}') for i in range(layout_store.MAX_SESSIONS_PER_SNAPSHOT)]
        self.assertIsNotNone(layout_store.build_snapshot('slot', sessions, now=NOW))

    def test_rejects_duplicate_session_names(self):
        sessions = [session_snap('web'), session_snap('web')]
        self.assertIsNone(layout_store.build_snapshot('slot', sessions, now=NOW))

    def test_rejects_combined_pane_count_over_the_snapshot_cap(self):
        # Each session is within MAX_TOTAL_PANES on its own, but together they
        # exceed MAX_PANES_PER_SNAPSHOT.
        big_windows = [{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                        'panes': [{'cwd': '/a', 'active': True}] * 100}]
        sessions = [session_snap('a', windows=big_windows), session_snap('b', windows=big_windows),
                   session_snap('c', windows=big_windows)]
        self.assertIsNone(layout_store.build_snapshot('slot', sessions, now=NOW))


class ConflictingSessionsTest(unittest.TestCase):

    def test_returns_names_that_are_live_in_snapshot_order(self):
        snap = snapshot(sessions=[session_snap('web'), session_snap('dev'), session_snap('ci')])

        self.assertEqual(layout_store.conflicting_sessions(snap, {'ci', 'web'}), ['web', 'ci'])

    def test_empty_when_nothing_is_live(self):
        snap = snapshot(sessions=[session_snap('web')])

        self.assertEqual(layout_store.conflicting_sessions(snap, set()), [])


class ValidateSnapshotTest(unittest.TestCase):

    def test_accepts_a_well_formed_snapshot(self):
        self.assertTrue(layout_store.validate_snapshot(snapshot()))

    def test_accepts_multiple_sessions(self):
        snap = snapshot(sessions=[session_snap('web', attached=True), session_snap('dev')])

        self.assertTrue(layout_store.validate_snapshot(snap))

    def test_rejects_bad_layout_string(self):
        for layout in ('', 'nope', '81f0,200x50;rm -rf /', 'zzzz,80x24,0,0,1',
                       '81f0,' + 'x' * layout_store.MAX_LAYOUT):
            snap = snapshot()
            snap['sessions'][0]['windows'][0]['layout'] = layout
            with self.subTest(layout=layout[:20]):
                self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_bad_session_name(self):
        for session in ('', 'a:b', 'a\nb', 'x' * 129, 42):
            snap = snapshot()
            snap['sessions'][0]['session'] = session
            with self.subTest(session=session):
                self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_non_bool_attached(self):
        for attached in (1, 'true', None, 0):
            snap = snapshot()
            snap['sessions'][0]['attached'] = attached
            with self.subTest(attached=attached):
                self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_duplicate_session_names(self):
        snap = snapshot(sessions=[session_snap('web'), session_snap('web')])

        self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_empty_or_oversized_sessions_list(self):
        empty = snapshot(); empty['sessions'] = []
        not_list = snapshot(); not_list['sessions'] = {}
        too_many = snapshot(sessions=[session_snap(f's{i}')
                                      for i in range(layout_store.MAX_SESSIONS_PER_SNAPSHOT + 1)])
        for snap in (empty, not_list, too_many):
            self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_combined_pane_count_over_the_snapshot_cap(self):
        big_windows = [{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                        'panes': [{'cwd': '/a', 'active': True}] * 100}]
        snap = snapshot(sessions=[session_snap('a', windows=big_windows),
                                  session_snap('b', windows=big_windows),
                                  session_snap('c', windows=big_windows)])

        self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_bad_structure(self):
        cases = []
        snap = snapshot(); snap['sessions'][0]['windows'] = {};               cases.append(snap)
        snap = snapshot(); snap['sessions'][0]['windows'] = [];               cases.append(snap)
        snap = snapshot(); snap['sessions'][0]['windows'][0]['panes'] = [];   cases.append(snap)
        snap = snapshot(); snap['sessions'][0]['windows'][0]['index'] = -1;   cases.append(snap)
        snap = snapshot(); snap['sessions'][0]['windows'][0]['index'] = True; cases.append(snap)
        snap = snapshot(); snap['sessions'][0]['width'] = 'wide';             cases.append(snap)
        for snap in cases:
            self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_relative_or_dirty_cwd(self):
        for cwd in ('', 'relative/path', '/a\nb', '/a\0b', '/' + 'x' * layout_store.MAX_CWD):
            snap = snapshot()
            snap['sessions'][0]['windows'][0]['panes'][0]['cwd'] = cwd
            with self.subTest(cwd=cwd[:20]):
                self.assertFalse(layout_store.validate_snapshot(snap))

    def test_rejects_non_dict(self):
        for value in (None, [], 'snap', 7):
            self.assertFalse(layout_store.validate_snapshot(value))

    def test_rejects_bad_slot_name(self):
        for name in ('', 'x' * 65, 'a\nb', 42):
            snap = snapshot(name=name)
            with self.subTest(name=name):
                self.assertFalse(layout_store.validate_snapshot(snap))


class BuildRestoreStepsTest(unittest.TestCase):

    def snap(self, windows, active_window=0):
        return {
            'session': 'web', 'attached': False,
            'width': 200, 'height': 50, 'pane_order': 'layout',
            'active_window': active_window, 'windows': windows,
        }

    def test_single_window_single_pane(self):
        snap = self.snap([{'index': 0, 'name': 'solo', 'active': True, 'layout': LAYOUT1,
                           'panes': [{'cwd': '/a', 'active': True}]}])
        steps = layout_store.build_restore_steps(snap, 'tmp1')

        self.assertEqual([s['op'] for s in steps],
                         ['new_session', 'move_first_window', 'layout',
                          'select_window', 'select_pane'])
        self.assertEqual(steps[0]['argv'],
                         ['new-session', '-d', '-P', '-F', '#{window_index}\t#{pane_id}',
                          '-s', 'tmp1', '-n', 'solo', '-c', '/a', '-x', '200', '-y', '50'])
        self.assertEqual(steps[0]['capture'], 'w0.p0')
        self.assertEqual(steps[1], {'op': 'move_first_window', 'session': 'tmp1', 'window': 0})
        self.assertEqual(steps[2]['argv'], ['select-layout', '-t', 'tmp1:0', LAYOUT1])
        self.assertEqual(steps[3]['argv'], ['select-window', '-t', 'tmp1:0'])
        self.assertEqual(steps[4]['argv'], ['select-pane', '-t', Ref('w0.p0')])

    def test_splits_chain_off_the_previous_pane(self):
        snap = self.snap([{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT3,
                           'panes': [{'cwd': '/a', 'active': False},
                                     {'cwd': '/b', 'active': True},
                                     {'cwd': '/c', 'active': False}]}])
        steps = layout_store.build_restore_steps(snap, 'tmp1')
        splits = [s for s in steps if s['op'] == 'split']

        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[0]['argv'],
                         ['split-window', '-d', '-P', '-F', '#{pane_id}',
                          '-t', Ref('w0.p0'), '-c', '/b'])
        self.assertEqual(splits[0]['capture'], 'w0.p1')
        self.assertEqual(splits[1]['argv'],
                         ['split-window', '-d', '-P', '-F', '#{pane_id}',
                          '-t', Ref('w0.p1'), '-c', '/c'])
        self.assertEqual(splits[1]['capture'], 'w0.p2')
        # The active pane is the second one in layout order.
        self.assertEqual(steps[-1]['argv'], ['select-pane', '-t', Ref('w0.p1')])

    def test_later_windows_are_created_at_their_saved_index(self):
        snap = self.snap([
            {'index': 0, 'name': 'first', 'active': False, 'layout': LAYOUT1,
             'panes': [{'cwd': '/a', 'active': True}]},
            {'index': 3, 'name': 'third', 'active': True, 'layout': LAYOUT1,
             'panes': [{'cwd': '/b', 'active': True}]},
        ], active_window=3)
        steps = layout_store.build_restore_steps(snap, 'tmp1')
        new_windows = [s for s in steps if s['op'] == 'new_window']

        self.assertEqual(len(new_windows), 1)
        self.assertEqual(new_windows[0]['argv'],
                         ['new-window', '-d', '-P', '-F', '#{pane_id}',
                          '-t', 'tmp1:3', '-n', 'third', '-c', '/b'])
        self.assertEqual(new_windows[0]['capture'], 'w1.p0')
        self.assertEqual(steps[-2]['argv'], ['select-window', '-t', 'tmp1:3'])
        self.assertEqual(steps[-1]['argv'], ['select-pane', '-t', Ref('w1.p0')])

    def test_move_first_window_targets_the_saved_index(self):
        snap = self.snap([{'index': 5, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                           'panes': [{'cwd': '/a', 'active': True}]}], active_window=5)
        steps = layout_store.build_restore_steps(snap, 'tmp1')

        self.assertEqual(steps[1], {'op': 'move_first_window', 'session': 'tmp1', 'window': 5})

    def test_restore_size_leaves_room_for_every_split(self):
        panes = [{'cwd': '/a', 'active': i == 0} for i in range(10)]
        snap = self.snap([{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                           'panes': panes}])
        snap['width'], snap['height'] = 40, 10

        self.assertEqual(layout_store.restore_size(snap), (40, 22))


class UniqueSessionNameTest(unittest.TestCase):

    def test_returns_base_when_free(self):
        self.assertEqual(layout_store.unique_session_name('web', set()), 'web')

    def test_appends_the_first_free_suffix(self):
        self.assertEqual(layout_store.unique_session_name('web', {'web'}), 'web-2')
        self.assertEqual(layout_store.unique_session_name('web', {'web', 'web-2'}), 'web-3')


class StoreIOTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'WEB_TMUX_STATE_DIR': self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_round_trip(self):
        store = layout_store.empty_store()
        snap = snapshot('作業用', sessions=[session_snap('web'), session_snap('dev')])
        self.assertTrue(layout_store.put_slot(store, snap))
        layout_store.save_store(store)

        self.assertEqual(layout_store.load_store()['slots']['作業用'], snap)

    def test_permissions_are_private(self):
        layout_store.save_store(layout_store.empty_store())
        path = layout_store.store_path()

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir.name).st_mode), 0o700)
        self.assertEqual(os.listdir(self.dir.name), [layout_store.STORE_FILENAME])

    def test_missing_store_reads_empty(self):
        self.assertEqual(layout_store.load_store(), layout_store.empty_store())

    def test_corrupt_store_reads_empty(self):
        with open(layout_store.store_path(), 'w', encoding='utf-8') as fh:
            fh.write('{not json')

        self.assertEqual(layout_store.load_store(), layout_store.empty_store())

    def test_foreign_version_reads_empty(self):
        with open(layout_store.store_path(), 'w', encoding='utf-8') as fh:
            json.dump({'version': 99, 'slots': {'a': snapshot('a')}}, fh)

        self.assertEqual(layout_store.load_store()['slots'], {})

    def test_version_1_store_reads_empty(self):
        # Pre-release schema change: a single-session (v1) store is not
        # migrated, just dropped.
        old_slot = {
            'name': 'a', 'session': 'web', 'saved_at': NOW,
            'width': 200, 'height': 50, 'pane_order': 'layout', 'active_window': 0,
            'windows': [{'index': 0, 'name': 'w', 'active': True, 'layout': LAYOUT1,
                         'panes': [{'cwd': '/a', 'active': True}]}],
        }
        with open(layout_store.store_path(), 'w', encoding='utf-8') as fh:
            json.dump({'version': 1, 'slots': {'a': old_slot}}, fh)

        self.assertEqual(layout_store.load_store()['slots'], {})

    def test_invalid_slots_are_dropped_on_load(self):
        bad = snapshot('bad')
        bad['sessions'][0]['windows'][0]['layout'] = 'not-a-layout'
        mismatched = snapshot('other')                 # key disagrees with name
        with open(layout_store.store_path(), 'w', encoding='utf-8') as fh:
            json.dump({'version': layout_store.SCHEMA_VERSION,
                       'slots': {'good': snapshot('good'), 'bad': bad, 'key': mismatched}}, fh)

        self.assertEqual(list(layout_store.load_store()['slots']), ['good'])

    def test_store_is_capped(self):
        store = layout_store.empty_store()
        for i in range(layout_store.MAX_SLOTS):
            self.assertTrue(layout_store.put_slot(store, snapshot(f'slot{i}')))

        self.assertFalse(layout_store.put_slot(store, snapshot('one-too-many')))
        # Replacing an existing slot still works at the cap.
        self.assertTrue(layout_store.put_slot(store, snapshot('slot0')))

    def test_summaries_are_newest_first(self):
        store = layout_store.empty_store()
        old = snapshot('old', saved_at='2026-01-01T00:00:00+09:00')
        new = snapshot('new', sessions=[session_snap('web'), session_snap('dev')],
                       saved_at='2026-09-17T00:00:00+09:00')
        layout_store.put_slot(store, old)
        layout_store.put_slot(store, new)

        summaries = layout_store.slot_summaries(store)
        self.assertEqual([s['name'] for s in summaries], ['new', 'old'])
        self.assertEqual(summaries[0], {'name': 'new', 'sessions': 2, 'windows': 2,
                                        'panes': 2, 'saved_at': new['saved_at']})


if __name__ == '__main__':
    unittest.main()
