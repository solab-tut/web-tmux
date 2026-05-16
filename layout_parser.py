#!/usr/bin/env python3
"""
Parse tmux layout strings into a flat list of pane geometries.

Layout string format:
  checksum,WxH,x,y,pane_id          (leaf pane)
  checksum,WxH,x,y{child,...}       (horizontal split, {})
  checksum,WxH,x,y[child,...]       (vertical split,   [])

All x/y coordinates are absolute within the terminal window.
"""
import re


def parse_layout(layout_str: str) -> list[dict]:
    """Return [{id: int, x, y, cols, rows}, ...] for each leaf pane."""
    idx = layout_str.find(',')
    if idx < 0:
        return []
    panes: list[dict] = []
    _parse_node(layout_str[idx + 1:], panes)
    return panes


def _parse_node(s: str, panes: list[dict]) -> None:
    m = re.match(r'(\d+)x(\d+),(\d+),(\d+)(.*)', s)
    if not m:
        return
    cols, rows = int(m.group(1)), int(m.group(2))
    x,    y    = int(m.group(3)), int(m.group(4))
    rest       = m.group(5)

    if not rest:
        return

    if rest[0] == ',':
        # Leaf: ,pane_id
        num = rest[1:].split(',')[0]
        if num.isdigit():
            panes.append({'id': int(num), 'x': x, 'y': y, 'cols': cols, 'rows': rows})
    elif rest[0] in ('{', '['):
        inner, _ = _extract_bracket(rest)
        for child in _split_children(inner):
            _parse_node(child, panes)


def _extract_bracket(s: str) -> tuple[str, str]:
    depth = 0
    for i, c in enumerate(s):
        if c in ('{', '['):
            depth += 1
        elif c in ('}', ']'):
            depth -= 1
            if depth == 0:
                return s[1:i], s[i + 1:]
    return s[1:], ''


def _split_children(s: str) -> list[str]:
    # Split only at commas that are followed by a new node header (\d+x\d+),
    # not at commas that are part of a leaf's (x, y, pane_id) sequence.
    parts, depth, start = [], 0, 0
    i = 0
    while i < len(s):
        c = s[i]
        if c in ('{', '['):
            depth += 1
        elif c in ('}', ']'):
            depth -= 1
        elif c == ',' and depth == 0 and re.match(r'\d+x\d+', s[i + 1:]):
            parts.append(s[start:i])
            start = i + 1
        i += 1
    if start < len(s):
        parts.append(s[start:])
    return parts


if __name__ == '__main__':
    # Quick smoke test
    cases = [
        '5963,80x24,0,0,14',
        '7b1a,80x24,0,0{40x24,0,0,14,39x24,41,0,15}',
        'abcd,80x24,0,0[80x12,0,0,0,80x11,0,13{40x11,0,13,1,39x11,41,13,2}]',
    ]
    for c in cases:
        print(c)
        for p in parse_layout(c):
            print(' ', p)
