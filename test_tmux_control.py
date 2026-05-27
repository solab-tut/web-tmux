import unittest

from tmux_control import (
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


if __name__ == '__main__':
    unittest.main()
