"""Exercise the viewer's request lifecycle using its actual JavaScript.

Run with Node.js or GJS (SpiderMonkey); no browser or live server is needed.
"""
from pathlib import Path
import shutil
import subprocess
import unittest


APP = (Path(__file__).parent / 'static/app.js').read_text()
JS = shutil.which('node') or shutil.which('nodejs') or shutil.which('gjs')


def function(name):
    start = APP.index(f'function {name}(')
    return APP[start:APP.index('\n}', start) + 2]


@unittest.skipUnless(JS, 'requires Node.js or GJS')
class FileViewerTest(unittest.TestCase):
    def run_js(self, scenario):
        declarations = APP[APP.index('const _files = {'):APP.index('function nextFileId()')]
        functions = '\n'.join(function(name) for name in (
            'nextFileId', 'joinPath', 'parentPath', 'documentPath',
            'sendFileRequest', 'requestFileDir', 'requestFile', 'reloadFileView',
            'fileReplyWanted', 'onFileContent', 'onMdImageReply', 'resetMdImages',
            'backToFileDir', 'onInit',
        ))
        harness = """
const assert = (ok) => { if (!ok) throw new Error('assertion failed'); };
const equal = (a, b) => assert(JSON.stringify(a) === JSON.stringify(b));
const loadFilesShowHidden = () => false;
const IMAGE_FILE_RE = /\\.(png|svg)$/i;
let currentHost = 'local', currentGen = 5, activePaneId = '%3';
let currentSession, _sessionNote, _editingWindowIndex;
const sent = [];
const wsSendType = (type, extra) => { sent.push({type, ...extra}); return true; };
const wsSend = () => true;
const document = {getElementById: () => ({})};
const b64ToUint8 = () => new Uint8Array();
const renderFileView = () => {};
const showFileNote = () => {};
const destroyAllPanes = () => {}, resetResizeCache = () => {};
const dropHostAuthPrompts = () => {}, updateSessions = () => {};
const renderWindowList = () => {}, renderPaneList = () => {};
const updateCurrentWindow = () => {}, applyLayout = () => {};
const scheduleSnapshotRefresh = () => {};
"""
        code = harness + declarations + functions + """
_files.open = true;
_files.host = 'local';
_files.dir = '/work';
_files.entries = [{name: 'README.md', kind: 'f'}];
_files.file = {path: '/work/README.md'};
""" + scenario
        flag = '-c' if Path(JS).name == 'gjs' else '-e'
        result = subprocess.run([JS, flag, code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reload_clears_both_successful_and_failed_images(self):
        self.run_js("""
_mdImages.doc = _files.file.path;
_mdImages.cache.set('/work/plot.png', {src: 'old image'});
_mdImages.cache.set('/work/missing.png', {error: 'not found'});
_mdImages.count = 1;
_mdImages.bytes = 100;
reloadFileView();
onFileContent({id: _files.reqId, host: 'local', path: '/work/README.md', size: 0});
equal(_mdImages.cache.size, 0);
equal(_mdImages.count, 0);
equal(_mdImages.bytes, 0);
""")

    def test_redraw_keeps_images(self):
        self.run_js("""
_mdImages.doc = _files.file.path;
_mdImages.cache.set('/work/plot.png', {src: 'cached image'});
resetMdImages(_files.file.path);
equal(_mdImages.cache.get('/work/plot.png').src, 'cached image');
""")

    def test_back_ignores_delayed_file_response(self):
        self.run_js("""
requestFile('/work/other.md');
const oldId = _files.reqId;
backToFileDir();
equal(_files.pending, null);
onFileContent({id: oldId, host: 'local', path: '/work/other.md', size: 0});
equal(_files.file, null);
""")

    def test_back_requests_directory_when_no_listing_is_cached(self):
        self.run_js("""
_files.entries = null;
requestFile('/work/other.md');
const oldId = _files.reqId;
backToFileDir();
equal(sent[sent.length - 1].type, 'list_dir');
equal(sent[sent.length - 1].path, '/work');
assert(_files.reqId !== oldId);
""")

    def test_reconnect_retries_pending_navigation(self):
        for request in (
            "requestFile('/work/other.md')",
            "requestFileDir('/another')",
            "requestFileDir('', '%7')",
        ):
            with self.subTest(request=request):
                self.run_js(request + ";" + """
const before = {...sent[sent.length - 1]};
onInit({session: 'dev'});
const after = {...sent[sent.length - 1]};
assert(after.id !== before.id);
delete before.id;
delete after.id;
equal(after, before);
""")

    def test_document_paths_preserve_host_symlink_resolution(self):
        self.run_js("""
equal(documentPath('link/../plot.png', '/work/README.md'), '/work/link/../plot.png');
equal(documentPath('../notes.md#heading', '/work/link/README.md'), '/work/link/../notes.md');
equal(documentPath('/other/link/../plot%20one.png?raw=1', '/work/README.md'),
      '/other/link/../plot one.png');
equal(documentPath('#heading', '/work/README.md'), '');
""")
