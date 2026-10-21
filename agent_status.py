#!/usr/bin/env python3
from __future__ import annotations

"""
What a coding agent in a pane is doing, read off its screen.

Claude Code, Codex and Hermes Agent tell no one but the terminal whether they
are working or waiting, so the sidebar reads the same screen the user would:
a spinner line or "esc to interrupt" while a turn runs, an approval dialog
when a tool call needs a yes. Nothing has to be set up on the host, and the
same rules work for local and ssh hosts alike.

The patterns follow what the agents print today (Claude Code 2.1, Codex CLI
0.163, Hermes Agent 0.22); an agent that changes its wording falls back to
"idle" until they are updated here.
"""
import re
from typing import NamedTuple

IDLE = 'idle'
THINKING = 'thinking'
# Waiting on the user: an approval, and also a choice or a value it asked for.
APPROVAL = 'approval'
# Idle, after a turn that ended and has not been acknowledged (Unseen).
DONE = 'done'

# Higher wins when one row stands for several panes (a window, a session).
STATE_RANK = {IDLE: 1, THINKING: 2, DONE: 3, APPROVAL: 4}

# Only the bottom of the screen counts. The agents redraw their dialogs and
# status lines down there, while the rows above may still show an old
# approval question or a document that happens to say "esc to interrupt".
TAIL_LINES = 30

# Commands an agent may run as. The agents' own names decide on their own;
# an interpreter is only an agent if its screen says so.
_AGENT_COMMANDS = {'claude': 'claude', 'codex': 'codex', 'hermes': 'hermes'}
_INTERPRETER_RE = re.compile(r'^(?:node|nodejs|bun|deno|uv|python[0-9.]*)$')

_I = re.IGNORECASE
_M = re.MULTILINE


class AgentRules(NamedTuple):
    # Seen in the tail when the agent runs under an interpreter.
    signature: re.Pattern
    approval: tuple[re.Pattern, ...]
    thinking: tuple[re.Pattern, ...]
    # pane_title forms, for the agents that put their state there.
    title_approval: re.Pattern | None = None
    title_thinking: re.Pattern | None = None
    # Where an approval dialog can be. A dialog is drawn at the very bottom,
    # and the same words in the agent's output above it (a quoted prompt, a
    # diff of this file) must not count: 'rule' looks only below the last
    # full-width ──── line, a number only at that many bottom rows.
    approval_region: str | int | None = None


AGENTS: dict[str, AgentRules] = {
    'claude': AgentRules(
        signature=re.compile(r'Claude Code|⏵⏵|⏸ manual mode|shift\+tab to cycle'),
        approval=(
            # "Do you want to create hello.txt?" / "…proceed?" / "…make this edit…"
            re.compile(r'^\s*Do you want to\b', _M),
            re.compile(r'^\s*❯\s*1\.\s*Yes\b', _M),
            # Folder trust, questions: "Enter to confirm · Esc to cancel"
            re.compile(r'Enter to (?:confirm|select)\b.*Esc to cancel'),
        ),
        thinking=(
            # "✶ Quantumizing…", "✢ Whirlpooling… (6m 12s · ↓ 13.9k tokens)".
            # A finished turn reads "✻ Baked for 4s", with no ellipsis.
            re.compile(r'^\s*[·✢*✳✶✻✽]\s+\S+…', _M),
            re.compile(r'esc to interrupt', _I),
        ),
        approval_region='rule',
    ),
    'codex': AgentRules(
        signature=re.compile(r'OpenAI Codex|Ask Codex|^\s*› ', _M),
        approval=(
            re.compile(r'^\s*›\s*1\.\s*Yes\b', _M),
            re.compile(r'Press enter to confirm or esc to cancel', _I),
        ),
        thinking=(
            # "• Working (3s • esc to interrupt)"
            re.compile(r'esc to interrupt', _I),
        ),
        title_approval=re.compile(r'Action Required'),
        # A braille spinner leads the title while a turn runs.
        title_thinking=re.compile(r'^[⠁-⣿]'),
    ),
    'hermes': AgentRules(
        signature=re.compile(r'☤'),
        approval=(
            re.compile(r'Dangerous Command'),
            re.compile(r'\bAllow once\b'),
            re.compile(r'needs your input'),
            re.compile(r'Confirm action|Type 1/2/3 or use'),
            # Boxes that wait for a choice or a value, not a yes: "⚙ Model
            # Picker — Select Provider", a sudo password, a skill's secret.
            # Each is gone from the screen once answered.
            re.compile(r'⚙ Model Picker —'),
            re.compile(r'Sudo Password Required|Skill Setup Required'),
            re.compile(r'🔐 (?:Verification code for|Save login for|Unlock) '),
        ),
        thinking=(
            # The input line while a turn runs: "☤ ❯ msg=interrupt · /queue …"
            re.compile(r'msg=interrupt'),
            re.compile(r'Ctrl\+C cancel'),
            # The status bar's running stopwatch; a finished turn shows ⏲.
            re.compile(r'│ ⏱ \d'),
        ),
    ),
}

# The order interpreters are checked in: Claude Code's screen can show a
# "›" too, so the more specific signatures go first.
_SIGNATURE_ORDER = ('hermes', 'claude', 'codex')

AGENT_NAMES = {'claude': 'Claude Code', 'codex': 'Codex', 'hermes': 'Hermes Agent'}


def is_candidate(command: str) -> bool:
    """Whether a pane running `command` is worth a screen capture."""
    return command in _AGENT_COMMANDS or bool(_INTERPRETER_RE.match(command))


def screen_tail(screen: str, lines: int = TAIL_LINES) -> str:
    """The last `lines` rows of a capture, blank rows at the bottom dropped."""
    rows = screen.rstrip('\n').split('\n')
    while rows and not rows[-1].strip():
        rows.pop()
    return '\n'.join(rows[-lines:])


# Claude Code draws its input box and its dialogs under one of these.
_RULE_RE = re.compile(r'^\s*─{20,}', _M)
# The input box's prompt line, not a dialog's "❯ 1. Yes".
_INPUT_BOX_RE = re.compile(r'\A\s*❯(?!\s*\d+\.)')


def approval_text(tail: str, region: str | int | None) -> str:
    if region == 'rule':
        rules = list(_RULE_RE.finditer(tail))
        if not rules:
            return tail
        below = tail[rules[-1].end():]
        # Some dialogs ("Teach auto mode about your environment?") sit above
        # the input box, in a rule of their own, while the box stays put.
        if (len(rules) >= 3
                and _INPUT_BOX_RE.match(tail[rules[-2].end():rules[-1].start()])):
            return tail[rules[-3].end():rules[-2].start()] + below
        return below
    if isinstance(region, int):
        return '\n'.join(tail.split('\n')[-region:])
    return tail


def identify(command: str, tail: str) -> str | None:
    agent = _AGENT_COMMANDS.get(command)
    if agent is not None:
        return agent
    if not _INTERPRETER_RE.match(command):
        return None
    for name in _SIGNATURE_ORDER:
        if AGENTS[name].signature.search(tail):
            return name
    return None


def classify(command: str, title: str, screen: str,
             known: str | None = None) -> tuple[str, str] | None:
    """(agent, state) for a pane, or None when no agent runs in it.

    `known` is the agent this pane was last seen running under the same
    command: an interpreter's screen loses its signature for a moment while
    the agent redraws, and the pane is still that agent meanwhile."""
    tail = screen_tail(screen)
    agent = identify(command, tail) or known
    if agent is None:
        return None
    rules = AGENTS[agent]
    dialog = approval_text(tail, rules.approval_region)
    if (any(p.search(dialog) for p in rules.approval)
            or (rules.title_approval is not None and rules.title_approval.search(title))):
        return agent, APPROVAL
    if (any(p.search(tail) for p in rules.thinking)
            or (rules.title_thinking is not None and rules.title_thinking.search(title))):
        return agent, THINKING
    return agent, IDLE


# ──────────────────────────────────────────── tmux output

# Title last: it is free text and may hold the separator itself. The tmux
# server's pid first: pane ids start again from %0 when the server restarts.
LIST_FORMAT = ('#{pid}|#{session_name}|#{window_index}|#{pane_id}'
               '|#{pane_current_command}|#{pane_title}')
# A session name may hold the separator too, and shift the fields after it.
# The pane id goes into a tmux command, so it must be nothing but an id.
_PANE_ID_RE = re.compile(r'%\d+')


class PaneInfo(NamedTuple):
    session: str
    window: int
    pane: str
    command: str
    title: str
    server: str = ''


def parse_pane_list(raw: str) -> list[PaneInfo]:
    """`list-panes -a -F LIST_FORMAT` output → one PaneInfo per pane."""
    panes = []
    for line in raw.splitlines():
        p = line.split('|', 5)
        if (len(p) < 6 or not p[0].isdigit() or not p[2].isdigit()
                or not _PANE_ID_RE.fullmatch(p[3])):
            continue
        panes.append(PaneInfo(p[1], int(p[2]), p[3], p[4], p[5], p[0]))
    return panes


def capture_args(panes: list[PaneInfo], marker: str) -> list[str]:
    """tmux arguments that print each pane's screen, each followed by a line
    reading `marker`: one tmux run for all of them, over one ssh call.

    The marker should be new each time. A screen may show any text, and a
    fixed one would let it claim the rows after it for another pane."""
    args = []
    for p in panes:
        if args:
            args.append(';')
        args += ['capture-pane', '-p', '-t', p.pane, ';', 'display-message', '-p', marker]
    return args


def split_captures(out: str, panes: list[PaneInfo], marker: str) -> dict[str, str] | None:
    """capture_args output → pane -> screen; None unless every pane is there."""
    parts = out.split(marker + '\n')
    if len(parts) != len(panes) + 1 or parts[-1]:
        return None
    return {p.pane: screen for p, screen in zip(panes, parts)}


def pane_states(panes: list[PaneInfo], screens: dict[str, str],
                known: dict[str, tuple[str, str]] | None = None) -> list[dict]:
    """The sidebar's entries: one per pane that runs an agent, in pane order.

    `known` maps a pane to the (command, agent) it was last identified as;
    it is updated in place, and panes no longer running an agent leave it."""
    if known is None:
        known = {}
    entries = []
    seen = set()
    for info in panes:
        screen = screens.get(info.pane)
        if screen is None:
            continue
        previous = known.get(info.pane)
        result = classify(info.command, info.title, screen,
                          previous[1] if previous and previous[0] == info.command else None)
        if result is None:
            continue
        agent, state = result
        known[info.pane] = (info.command, agent)
        seen.add(info.pane)
        entries.append({
            'pane': info.pane,
            'session': info.session,
            'window': info.window,
            'agent': agent,
            'state': state,
        })
    for pane in set(known) - seen:
        del known[pane]
    return entries


# ──────────────────────────────────────────── finished, not yet acknowledged

# The control client's own session and window: what every browser shows.
VIEW_FORMAT = '#{session_name}|#{window_index}|#{window_zoomed_flag}|#{pane_id}'


def viewed_panes(view: str, panes: list[PaneInfo]) -> set[str] | None:
    """The panes on screen, from `display-message -p VIEW_FORMAT` output:
    the whole current window, or only its active pane while it is zoomed.
    None if the output can't be read, which says nothing either way."""
    p = view.strip().split('|')
    if len(p) != 4 or not p[1].isdigit():
        return None
    session, window, zoomed, active = p[0], int(p[1]), p[2] == '1', p[3]
    return {info.pane for info in panes
            if info.session == session and info.window == window
            and (not zoomed or info.pane == active)}


class Unseen:
    """Marks an agent DONE when a turn ends, until someone acknowledges it.

    A pane that goes from thinking or waiting to idle reads DONE instead of
    idle, on screen or not: the browser may be in a background tab, or no
    one in front of it. A key typed into the pane clears that, and so does
    its window leaving the screen once it has been on it, as does the agent
    starting again."""

    def __init__(self):
        self.last: dict[str, str] = {}   # pane -> its state at the last look
        self.panes: set[str] = set()     # panes finished and not yet acknowledged
        self.shown: set[str] = set()     # those of them on screen since they finished

    def apply(self, entries: list[dict], seen: set[str] | None) -> None:
        """`seen` is the panes on screen, or None when that is not known
        (no browser connected): then nothing counts as shown or as left."""
        for e in entries:
            pane, state = e['pane'], e['state']
            if state != IDLE:
                self._clear(pane)
            elif self.last.get(pane) in (THINKING, APPROVAL):
                self.panes.add(pane)
            if pane in self.panes and seen is not None:
                if pane in seen:
                    self.shown.add(pane)
                elif pane in self.shown:
                    self._clear(pane)   # looked at, then left
            self.last[pane] = state
            if pane in self.panes:
                e['state'] = DONE
        present = {e['pane'] for e in entries}
        for pane in set(self.last) - present:
            del self.last[pane]
        self.panes &= present
        self.shown &= present

    def typed(self, pane: str) -> bool:
        """A key typed into `pane`; whether that cleared a DONE."""
        cleared = pane in self.panes
        self._clear(pane)
        return cleared

    def _clear(self, pane: str) -> None:
        self.panes.discard(pane)
        self.shown.discard(pane)


class HostAgents:
    """One host's agents, kept while the browser looks at other hosts: the
    active host is read through the control client, the others over ssh,
    and a host keeps what it knew when it changes from one to the other."""

    def __init__(self):
        self.server = ''   # the tmux server's pid, see LIST_FORMAT
        self.known: dict[str, tuple[str, str]] = {}
        self.unseen = Unseen()
        self.entries: list[dict] = []
        self.checked_at = 0.0

    def update(self, panes: list[PaneInfo], screens: dict[str, str],
               seen: set[str] | None, now: float) -> None:
        server = next((p.server for p in panes), self.server)
        if server != self.server:
            # Pane ids name other panes now.
            self.server, self.known, self.unseen = server, {}, Unseen()
        self.entries = pane_states(panes, screens, self.known)
        self.unseen.apply(self.entries, seen)
        self.checked_at = now

    def typed(self, pane: str) -> bool:
        """A key typed into `pane`; whether its entry changed."""
        if not self.unseen.typed(pane):
            return False
        for e in self.entries:
            if e['pane'] == pane:
                e['state'] = IDLE
        return True
