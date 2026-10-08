import asyncio
import base64
import os
import json
import unittest
from unittest import mock

from tmux_control import (
    CLIPBOARD_MAX_BYTES,
    TmuxControl,
    _parse_pane_state,
    _strip_terminal_response_sequences,
    _strip_terminal_response_sequences_stream,
)


class TerminalResponseFilterTest(unittest.TestCase):
    def test_strips_complete_cpr_response(self):
        self.assertEqual(
            _strip_terminal_response_sequences(b'left\x1b[12;34Rright'),
            b'leftright',
        )

    def test_strips_cpr_response_split_across_chunks(self):
        first, rem = _strip_terminal_response_sequences_stream(b'left\x1b[12;')
        self.assertEqual(first, b'left')
        self.assertEqual(rem, b'\x1b[12;')

        second, rem = _strip_terminal_response_sequences_stream(b'34Rright', rem)
        self.assertEqual(second, b'right')
        self.assertEqual(rem, b'')

    def test_strips_osc_response_split_across_chunks(self):
        first, rem = _strip_terminal_response_sequences_stream(b'a\x1b]11;rgb:2e')
        self.assertEqual(first, b'a')
        self.assertEqual(rem, b'\x1b]11;rgb:2e')

        second, rem = _strip_terminal_response_sequences_stream(b'2e/3434/4040\x1b\\b', rem)
        self.assertEqual(second, b'b')
        self.assertEqual(rem, b'')

    def test_strips_device_attribute_response(self):
        self.assertEqual(
            _strip_terminal_response_sequences(b'a\x1b[?1;2cb'),
            b'ab',
        )

    def test_preserves_display_color_sequence(self):
        self.assertEqual(
            _strip_terminal_response_sequences(b'a\x1b[31mred\x1b[0mb'),
            b'a\x1b[31mred\x1b[0mb',
        )


class _Recorder:
    def __init__(self, events):
        self.events = events

    def enqueue(self, data):
        self.events.append(('broadcast', data))
        return True


class SnapshotOrderingTest(unittest.TestCase):
    """A snapshot must sit in the stream exactly where tmux captured it."""

    def _run(self, lines):
        async def scenario():
            tc = TmuxControl()
            read_fd, write_fd = os.pipe()
            tc._master_fd = write_fd

            async def connected():
                return None
            tc._ensure_connected = connected
            events = []
            tc.subscribers.append(_Recorder(events))
            task = asyncio.create_task(
                tc.snapshot_pane('%1', lambda snap: events.append(('snapshot', snap)))
            )
            await asyncio.sleep(0)
            sent = os.read(read_fd, 4096).decode()
            # One PTY read carries many lines and _on_readable handles them all
            # before the awaiting coroutine can resume, so delivery has to happen
            # inside _handle_line, not after the await returns.
            for line in lines:
                tc._handle_line(line)
            await task
            os.close(read_fd)
            os.close(write_fd)
            return sent, events
        return asyncio.run(scenario())

    def test_display_and_capture_go_out_on_one_line(self):
        sent, _ = self._run([
            '%begin 1 1 1', '0|0|80|24|0|0|23|0|1|0|0|0|1', '%end 1 1 1',
            '%begin 1 2 1', '%end 1 2 1',
        ])
        self.assertEqual(sent.count('\n'), 1)
        self.assertIn('display-message -p -t %1', sent)
        self.assertIn(' ; capture-pane -t %1 -p -e -N -S 0', sent)

    def test_snapshot_is_delivered_between_output_before_and_after_capture(self):
        _, events = self._run([
            '%output %1 before',
            '%begin 1 1 1', '3|4|80|24|1|2|20|1|0|1|1|1|0', '%end 1 1 1',
            '%begin 1 2 1', 'row1', 'row2', '%end 1 2 1',
            '%output %1 after',
        ])
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ['broadcast', 'snapshot', 'broadcast'])
        self.assertIn(base64.b64encode(b'before').decode(), events[0][1])
        self.assertIn(base64.b64encode(b'after').decode(), events[2][1])
        snap = events[1][1]
        self.assertEqual(snap['data'], b'row1\nrow2')
        self.assertEqual(
            {k: snap[k] for k in ('cursor_x', 'cursor_y', 'pane_cols', 'pane_rows')},
            {'cursor_x': 3, 'cursor_y': 4, 'pane_cols': 80, 'pane_rows': 24},
        )
        self.assertEqual(snap['alternate_on'], 1)
        self.assertEqual((snap['scroll_region_upper'], snap['scroll_region_lower']), (2, 20))
        self.assertEqual(snap['wrap_flag'], 0)
        self.assertEqual(snap['cursor_flag'], 0)

    def test_missing_pane_delivers_nothing(self):
        _, events = self._run([
            "%begin 1 1 1", "can't find pane: %1", '%error 1 1 1',
            "%begin 1 2 1", "can't find pane: %1", '%error 1 2 1',
        ])
        self.assertEqual(events, [])


class PaneStateParseTest(unittest.TestCase):
    def test_missing_fields_fall_back_to_terminal_defaults(self):
        state = _parse_pane_state('5|6|80|24')
        self.assertEqual(state['cursor_x'], 5)
        self.assertEqual(state['pane_rows'], 24)
        self.assertEqual(state['wrap_flag'], 1)
        self.assertEqual(state['cursor_flag'], 1)
        self.assertEqual(state['scroll_region_lower'], -1)
        self.assertEqual(state['alternate_on'], 0)
        self.assertEqual(state['mouse_all_flag'], 0)
        self.assertEqual(state['mouse_sgr_flag'], 0)

    def test_mouse_modes_are_read_in_order(self):
        state = _parse_pane_state('0|0|80|24|1|0|23|0|1|0|0|0|1|0|0|1|1')
        self.assertEqual(
            {k: state[k] for k in ('mouse_standard_flag', 'mouse_button_flag',
                                   'mouse_all_flag', 'mouse_sgr_flag')},
            {'mouse_standard_flag': 0, 'mouse_button_flag': 0,
             'mouse_all_flag': 1, 'mouse_sgr_flag': 1},
        )


class _FakeSaveBuffer:
    def __init__(self, data, returncode=0, delay=0.0):
        self.data = data
        self.returncode = returncode
        self.delay = delay

    async def communicate(self):
        await asyncio.sleep(self.delay)
        return self.data, b''


class ClipboardForwardTest(unittest.TestCase):
    """A new tmux paste buffer goes out to the browsers as a clipboard message."""

    def _run(self, lines, buffers):
        async def scenario():
            tc = TmuxControl()
            events = []
            tc.subscribers.append(_Recorder(events))
            calls = []

            async def fake_exec(*args, **kwargs):
                calls.append(args)
                return buffers[args[3]]

            with mock.patch('tmux_control.asyncio.create_subprocess_exec', fake_exec):
                for line in lines:
                    tc._handle_line(line)
                while tc._clipboard_tasks:
                    await asyncio.gather(*tc._clipboard_tasks)
            return calls, [json.loads(data) for _, data in events]
        return asyncio.run(scenario())

    def test_buffer_is_forwarded_with_exact_bytes(self):
        calls, msgs = self._run(
            ['%paste-buffer-changed buffer3'],
            {'buffer3': _FakeSaveBuffer('line1\n日本語\n'.encode())},
        )
        self.assertEqual(calls, [('tmux', 'save-buffer', '-b', 'buffer3', '-')])
        self.assertEqual(msgs, [{'type': 'clipboard', 'text': 'line1\n日本語\n'}])

    def test_only_the_latest_copy_is_forwarded(self):
        _, msgs = self._run(
            ['%paste-buffer-changed slow', '%paste-buffer-changed fast'],
            {
                'slow': _FakeSaveBuffer(b'old', delay=0.05),
                'fast': _FakeSaveBuffer(b'new'),
            },
        )
        self.assertEqual(msgs, [{'type': 'clipboard', 'text': 'new'}])

    def test_oversized_or_missing_buffer_is_not_forwarded(self):
        _, msgs = self._run(
            ['%paste-buffer-changed big'],
            {'big': _FakeSaveBuffer(b'x' * (CLIPBOARD_MAX_BYTES + 1))},
        )
        self.assertEqual(msgs, [])
        _, msgs = self._run(
            ['%paste-buffer-changed gone'],
            {'gone': _FakeSaveBuffer(b'', returncode=1)},
        )
        self.assertEqual(msgs, [])


if __name__ == '__main__':
    unittest.main()
