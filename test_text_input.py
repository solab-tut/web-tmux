"""Check how replaced text (iOS dictation) is turned into terminal input.

Run with Node.js or GJS (SpiderMonkey); no browser or live server is needed.
"""
from pathlib import Path
import json
import shutil
import subprocess
import unittest


APP = (Path(__file__).parent / 'static/app.js').read_text()
JS = shutil.which('node') or shutil.which('nodejs') or shutil.which('gjs')


def function(name):
    start = APP.index(f'function {name}(')
    return APP[start:APP.index('\n}', start) + 2]


@unittest.skipUnless(JS, 'requires Node.js or GJS')
class TextReplacementInputTest(unittest.TestCase):
    def replacement(self, replaced, data):
        code = function('textReplacementInput') + f"""
print(JSON.stringify(textReplacementInput({json.dumps(replaced)}, {json.dumps(data)})));
"""
        if Path(JS).name != 'gjs':
            code = 'const print = console.log;\n' + code
        flag = '-c' if Path(JS).name == 'gjs' else '-e'
        result = subprocess.run([JS, flag, code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_a_longer_guess_sends_only_the_new_tail(self):
        self.assertEqual(self.replacement('音声', '音声入力'), '入力')
        self.assertEqual(self.replacement('he', 'hello'), 'llo')

    def test_the_same_guess_sends_nothing(self):
        self.assertEqual(self.replacement('hello', 'hello'), '')

    def test_a_changed_guess_deletes_back_to_where_it_differs(self):
        self.assertEqual(self.replacement('音声入路', '音声入力'), '\x7f力')
        self.assertEqual(self.replacement('です', 'でした。'), '\x7fした。')

    def test_characters_outside_the_bmp_count_once(self):
        self.assertEqual(self.replacement('𠮷野', '𠮷田'), '\x7f田')
        self.assertEqual(self.replacement('𠮷', '吉'), '\x7f吉')


if __name__ == '__main__':
    unittest.main()
