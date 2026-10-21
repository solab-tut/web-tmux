import unittest

import agent_status as a

RULE = '─' * 60

# The screens below are cut down from real captures (Claude Code 2.1.304,
# Codex CLI 0.163.0, Hermes Agent 0.22.2), blank rows and banners trimmed.

CLAUDE_IDLE = f'''\
 ▐▛███▛█   Claude Code v2.1.304
❯ Create a file named hello.txt containing the word hi.
● Write(hello.txt)
  ⎿  Wrote 1 line to hello.txt
✻ Baked for 4s · done 23:24
{RULE}
❯
{RULE}
  ⏸ manual mode on
'''

CLAUDE_THINKING = f'''\
❯ Create a file named hello.txt containing the word hi.
✶ Quantumizing…
{RULE}
❯
{RULE}
  ⏸ manual mode on
'''

CLAUDE_THINKING_STATS = f'''\
✢ Whirlpooling… (6m 16s · ↓ 14.2k tokens · thought for 1s)

{RULE}
❯
{RULE}

  ⏵⏵ auto mode on (shift+tab to cycle)
'''

CLAUDE_EDIT_APPROVAL = f'''\
● Write(hello.txt)
{RULE}
 Create file
 hello.txt
{'╌' * 60}
  1 hi
{'╌' * 60}
 Do you want to create hello.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits for this session (shift+tab)
   3. No
 Esc to cancel · Tab to amend
'''

CLAUDE_BASH_APPROVAL = f'''\
  ⎿  $ touch made-by-bash.txt
{RULE}
 Bash command
 touch made-by-bash.txt
 Do you want to proceed?
 ❯ 1. Yes
   4. No
 Esc to cancel · Tab to amend
'''

CLAUDE_TRUST = f'''\
{RULE}
 Accessing workspace:
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
'''

# Claude Code 2.1.305: a dialog in a rule of its own above the input box,
# which keeps a message typed before it came up.
CLAUDE_DIALOG_ABOVE_BOX = f'''\
  修正案: parse_pane_list で re.fullmatch(r'%\\d+', p[2]) を必須にします。
{RULE}
  Teach auto mode about your environment?

  Auto mode works better when it knows your environment. Takes about a minute.

  ❯ 1. Yes
    2. Not now
    3. Don't show again

  Enter to confirm · Esc to cancel
{RULE}
❯ はい、修正してテストも追加して
{RULE}

  ⏵⏵ auto mode on (shift+tab to cycle)
'''

CODEX_IDLE = '''\
  >_ OpenAI Codex (v0.163.0)
› Create a file named hello2.txt containing the word hi using a shell command.
• Created hello2.txt containing hi.
  Worked for 3m 32s • 11:24 PM
› Ask Codex to do anything
  GPT-6.1-Sol default · /tmp/work · Create hello2.txt
  ? for shortcuts
'''

CODEX_THINKING = '''\
› Create a file named hello2.txt containing the word hi using a shell command.
• I’ll create hello2.txt containing hi.
• Working (3s • esc to interrupt)
› Ask Codex to do anything
  GPT-6.1-Sol default · /tmp/work · Create hello2.txt
  ? for shortcuts
'''

CODEX_APPROVAL = '''\
• Running printf 'hi\\n' > hello2.txt
  Would you like to run the following command?
  Environment: local
  Reason: Allow creating hello2.txt containing hi in the workspace?
  $ printf 'hi\\n' > hello2.txt
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `printf` (p)
  3. No, and tell Codex what to do differently (esc)
  Press enter to confirm or esc to cancel
'''

HERMES_IDLE = f'''\
╭─ ☤ Hermes {'─' * 40}╮
Ran rm -rf ./junk successfully (exit code 0).
╰{'─' * 50}╯
 ☤ gpt-6.1-sol │ ~7.91K/500K │ [░░░░░░░░░░] ~2% │ 2m │ ⏲ 8s │ ✓ 0s
{RULE}
❯ What changed in this repo recently?
{RULE}
'''

HERMES_THINKING = f'''\
● Run this exact shell command with the terminal tool: rm -rf ./junk
Initializing agent...
  ◉_◉ contemplating...
 ☤ gpt-6.1-sol │ 0/500K │ [░░░░░░░░░░] 0% │ 2m │ ⏱ 0s
{RULE}
☤ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
{RULE}
'''

# Typing while a turn runs replaces the hint; the stopwatch still runs.
HERMES_THINKING_TYPING = f'''\
  💻 rm -rf ./junk  (  2.2s · ↓ 22 tok)
 ☤ gpt-6.1-sol │ 7.73K/500K │ [░░░░░░░░░░] 2% │ ◷ 3.1s │ 2m │ ⏱ 5s
{RULE}
☤ ❯ and then list the folder
{RULE}
'''

HERMES_APPROVAL = f'''\
╭─ ⚠️  Dangerous Command {'─' * 30}╮
│ chmod -R 777 ./x                                  │
│ world/other-writable permissions                  │
│ ❯ Allow once                                      │
│   Allow for this session                          │
│   Deny                                            │
╰{'─' * 50}╯
 ☤ gpt-6.1-sol │ 8.03K/500K │ [░░░░░░░░░░] 2% │ 4m │ ⏱ 4s
{RULE}
☤ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
{RULE}
'''

# /model leaves the status bar behind its box, so the ☤ is out of the tail.
HERMES_MODEL_PICKER = f'''\
Welcome to Hermes Agent! Type your message or /help for commands.
╭─ ⚙ Model Picker — Select Provider {'─' * 24}╮
│ Current: gpt-6.1-sol on ChatGPT or Codex Subscription     │
│ ❯ ChatGPT or Codex Subscription (14 models)  ← current    │
│   azure (2 models)                                        │
│   Cancel                                                  │
╰{'─' * 59}╯
{RULE}
❯ Set a reminder or schedule a recurring task
{RULE}
'''


class ClassifyTest(unittest.TestCase):
    def assertState(self, command, screen, expected, title=''):
        self.assertEqual(a.classify(command, title, screen), expected)

    def test_claude_code(self):
        self.assertState('claude', CLAUDE_IDLE, ('claude', a.IDLE))
        self.assertState('claude', CLAUDE_THINKING, ('claude', a.THINKING))
        self.assertState('claude', CLAUDE_THINKING_STATS, ('claude', a.THINKING))
        self.assertState('claude', CLAUDE_EDIT_APPROVAL, ('claude', a.APPROVAL))
        self.assertState('claude', CLAUDE_BASH_APPROVAL, ('claude', a.APPROVAL))
        self.assertState('claude', CLAUDE_TRUST, ('claude', a.APPROVAL))
        self.assertState('claude', CLAUDE_DIALOG_ABOVE_BOX, ('claude', a.APPROVAL))

    def test_claude_spinner_includes_a_plain_asterisk(self):
        screen = CLAUDE_THINKING.replace('✶ Quantumizing…', '* Quantumizing… (2s · ↓ 5 tokens)')
        self.assertState('claude', screen, ('claude', a.THINKING))

    def test_codex(self):
        self.assertState('codex', CODEX_IDLE, ('codex', a.IDLE), 'Create hello2.txt | work')
        self.assertState('codex', CODEX_THINKING, ('codex', a.THINKING))
        self.assertState('codex', CODEX_APPROVAL, ('codex', a.APPROVAL))

    def test_codex_title_alone_tells_its_state(self):
        self.assertState('codex', CODEX_IDLE, ('codex', a.THINKING), '⠏ Create hello2.txt | work')
        self.assertState('codex', CODEX_IDLE, ('codex', a.APPROVAL),
                         '[ . ] Action Required | Create hello2.txt | work')

    def test_hermes_runs_under_python(self):
        self.assertState('python3', HERMES_IDLE, ('hermes', a.IDLE))
        self.assertState('python3', HERMES_THINKING, ('hermes', a.THINKING))
        self.assertState('python3', HERMES_THINKING_TYPING, ('hermes', a.THINKING))
        self.assertState('python3', HERMES_APPROVAL, ('hermes', a.APPROVAL))
        self.assertState('hermes', HERMES_IDLE, ('hermes', a.IDLE))

    def test_hermes_waiting_for_a_choice(self):
        self.assertState('hermes', HERMES_MODEL_PICKER, ('hermes', a.APPROVAL))
        self.assertEqual(a.classify('python3', '', HERMES_MODEL_PICKER, known='hermes'),
                         ('hermes', a.APPROVAL))
        # Once picked, only the result line is left.
        switched = HERMES_IDLE.replace('Ran rm', '  ✓ Model switched: gpt-6.1-sol\nRan rm')
        self.assertState('python3', switched, ('hermes', a.IDLE))

    def test_agents_under_node_are_told_apart(self):
        self.assertState('node', CLAUDE_IDLE, ('claude', a.IDLE))
        self.assertState('node', CODEX_THINKING, ('codex', a.THINKING))

    def test_panes_without_an_agent(self):
        self.assertIsNone(a.classify('bash', '', 'ohno@host:~$ '))
        self.assertIsNone(a.classify('vim', '', CLAUDE_EDIT_APPROVAL))
        # less showing a document about these very words
        self.assertIsNone(a.classify('less', '', CODEX_THINKING))
        self.assertIsNone(a.classify('python3', '', '>>> print("esc to interrupt")\n'))

    def test_only_the_bottom_of_the_screen_counts(self):
        old_prompt = CLAUDE_EDIT_APPROVAL + 'output\n' * a.TAIL_LINES
        self.assertState('claude', old_prompt + CLAUDE_IDLE, ('claude', a.IDLE))

    def test_approval_words_in_claudes_output_are_not_a_dialog(self):
        # Claude printing a capture of another pane's dialog, above its own box.
        quoted = CLAUDE_BASH_APPROVAL.replace(RULE + '\n', '') + CLAUDE_IDLE.split('✻')[1]
        self.assertState('claude', '● Bash(cat capture.txt)\n' + quoted, ('claude', a.IDLE))

    def test_approval_words_above_claudes_box_need_a_rule_of_their_own(self):
        # The same dialog quoted in Claude's output, with no rule between it
        # and the box, nor one above it.
        quoted = CLAUDE_DIALOG_ABOVE_BOX.replace(RULE + '\n', '', 1)
        self.assertState('claude', quoted, ('claude', a.IDLE))

    def test_approval_words_in_codex_output_are_not_a_dialog(self):
        screen = '• Would you like to run the tests next?\n' + CODEX_IDLE
        self.assertState('codex', screen, ('codex', a.IDLE))

    def test_a_redraw_without_the_signature_keeps_the_known_agent(self):
        blank = 'Welcome to Hermes Agent! Type your message or /help for commands.\n'
        self.assertIsNone(a.classify('python3', '', blank))
        self.assertEqual(a.classify('python3', '', blank, known='hermes'), ('hermes', a.IDLE))


class PaneListTest(unittest.TestCase):
    RAW = '\n'.join([
        '700|web|0|%8|claude|✳ sidebar-agent-status',
        '700|web|1|%9|bash|MS-A2',
        '700|work|2|%12|codex|[ . ] Action Required | Create hello2.txt | work',
        'broken line',
    ])

    def test_parse_keeps_bars_in_the_title(self):
        panes = a.parse_pane_list(self.RAW)
        self.assertEqual([p.pane for p in panes], ['%8', '%9', '%12'])
        self.assertEqual(panes[2].title, '[ . ] Action Required | Create hello2.txt | work')
        self.assertEqual(panes[2].window, 2)
        self.assertEqual(panes[2].server, '700')

    def test_parse_drops_a_pane_id_shifted_by_bars_in_the_session_name(self):
        # It would go into "capture-pane -t …" and run as a command of its own.
        raw = '7|x|0|%0 ; new-session -d -s PWNED|node|0|%0|sleep|MS-A2\n7|web|0|%8|claude|t'
        self.assertEqual([p.pane for p in a.parse_pane_list(raw)], ['%8'])

    def test_candidates(self):
        for command in ('claude', 'codex', 'hermes', 'node', 'python3', 'python3.12', 'bun'):
            self.assertTrue(a.is_candidate(command), command)
        for command in ('bash', 'zsh', 'vim', 'less', 'nodemon', ''):
            self.assertFalse(a.is_candidate(command), command)

    def test_pane_states(self):
        panes = a.parse_pane_list(self.RAW)
        screens = {'%8': CLAUDE_THINKING, '%12': CODEX_IDLE}
        self.assertEqual(a.pane_states(panes, screens), [
            {'pane': '%8', 'session': 'web', 'window': 0, 'agent': 'claude', 'state': a.THINKING},
            {'pane': '%12', 'session': 'work', 'window': 2, 'agent': 'codex', 'state': a.APPROVAL},
        ])

    def test_known_agents_follow_the_panes(self):
        panes = [a.PaneInfo('s', 0, '%1', 'python3', ''), a.PaneInfo('s', 0, '%2', 'python3', '')]
        known = {}
        a.pane_states(panes, {'%1': HERMES_IDLE, '%2': HERMES_IDLE}, known)
        self.assertEqual(known, {'%1': ('python3', 'hermes'), '%2': ('python3', 'hermes')})
        # %1 redraws without its status bar; %2 is gone.
        entries = a.pane_states(panes[:1], {'%1': 'Initializing agent...\n'}, known)
        self.assertEqual([e['agent'] for e in entries], ['hermes'])
        self.assertEqual(known, {'%1': ('python3', 'hermes')})
        # The pane now runs another python program: no longer Hermes.
        other = [a.PaneInfo('s', 0, '%1', 'python3.12', '')]
        self.assertEqual(a.pane_states(other, {'%1': 'Initializing agent...\n'}, known), [])
        self.assertEqual(known, {})


class UnseenTest(unittest.TestCase):
    PANES = [a.PaneInfo('web', 0, '%1', 'claude', ''), a.PaneInfo('web', 0, '%2', 'codex', ''),
             a.PaneInfo('web', 1, '%3', 'claude', '')]

    def test_viewed_panes(self):
        self.assertEqual(a.viewed_panes('web|0|0|%2\n', self.PANES), {'%1', '%2'})
        self.assertEqual(a.viewed_panes('web|0|1|%2', self.PANES), {'%2'})   # zoomed
        self.assertEqual(a.viewed_panes('ops|0|0|%7', self.PANES), set())
        self.assertEqual(a.viewed_panes('', self.PANES), set())

    def look(self, unseen, states, seen=()):
        entries = [{'pane': p, 'state': s} for p, s in states.items()]
        unseen.apply(entries, set(seen))
        return {e['pane']: e['state'] for e in entries}

    def test_a_turn_ending_off_screen_stays_done_until_seen(self):
        u = a.Unseen()
        self.look(u, {'%1': a.THINKING, '%3': a.APPROVAL})
        self.assertEqual(self.look(u, {'%1': a.IDLE, '%3': a.IDLE}),
                         {'%1': a.DONE, '%3': a.DONE})
        self.assertEqual(self.look(u, {'%1': a.IDLE, '%3': a.IDLE}, seen={'%1'}),
                         {'%1': a.IDLE, '%3': a.DONE})
        self.assertEqual(self.look(u, {'%1': a.IDLE, '%3': a.IDLE}), {'%1': a.IDLE, '%3': a.DONE})
        # Started again: no longer done, and done again only after this turn.
        self.assertEqual(self.look(u, {'%3': a.THINKING}), {'%3': a.THINKING})
        self.assertEqual(u.panes, set())
        self.assertEqual(u.last, {'%3': a.THINKING})

    def test_a_turn_ending_on_screen_is_just_idle(self):
        u = a.Unseen()
        self.look(u, {'%1': a.THINKING}, seen={'%1'})
        self.assertEqual(self.look(u, {'%1': a.IDLE}, seen={'%1'}), {'%1': a.IDLE})
        # An agent first seen idle has no turn to report.
        self.assertEqual(self.look(u, {'%2': a.IDLE}), {'%2': a.IDLE})



class RemoteCaptureTest(unittest.TestCase):
    PANES = [a.PaneInfo('s', 0, '%1', 'claude', ''), a.PaneInfo('s', 1, '%4', 'node', '')]

    def test_capture_args(self):
        self.assertEqual(a.capture_args(self.PANES, 'M'), [
            'capture-pane', '-p', '-t', '%1', ';', 'display-message', '-p', 'M', ';',
            'capture-pane', '-p', '-t', '%4', ';', 'display-message', '-p', 'M'])

    def test_split_captures(self):
        out = 'one\n\nM\ntwo\nM\n'
        self.assertEqual(a.split_captures(out, self.PANES, 'M'), {'%1': 'one\n\n', '%4': 'two\n'})

    def test_split_captures_wants_every_pane_and_nothing_after(self):
        self.assertIsNone(a.split_captures('one\nM\n', self.PANES, 'M'))
        self.assertIsNone(a.split_captures('one\nM\ntwo\nM\nM\n', self.PANES, 'M'))
        self.assertIsNone(a.split_captures('one\nM\ntwo\nM\nthree\n', self.PANES, 'M'))


class HostAgentsTest(unittest.TestCase):
    def test_a_restarted_tmux_server_forgets_what_its_pane_ids_meant(self):
        agents = a.HostAgents()
        panes = [a.PaneInfo('s', 0, '%0', 'claude', '', '100')]
        agents.update(panes, {'%0': CLAUDE_THINKING}, set(), 1.0)
        self.assertEqual([e['state'] for e in agents.entries], [a.THINKING])
        self.assertEqual(agents.checked_at, 1.0)
        # Same server: the turn ended off screen.
        agents.update(panes, {'%0': CLAUDE_IDLE}, set(), 2.0)
        self.assertEqual([e['state'] for e in agents.entries], [a.DONE])
        # A new server whose %0 is another, idle Claude: not finished.
        agents.update([panes[0]._replace(server='200')], {'%0': CLAUDE_IDLE}, set(), 3.0)
        self.assertEqual([e['state'] for e in agents.entries], [a.IDLE])


if __name__ == '__main__':
    unittest.main()
