#!/usr/bin/env python3
"""Generate per-framework Objective-C class macros and Mach-O export allowlists for DmShellGame.

Objective-C registers classes in a process-wide flat table even when frameworks are loaded with
dlopen(..., RTLD_LOCAL). Giving each shell game framework its own DmFw_<NS_TAG>_<Class> prefix
and restricting global Mach-O exports to the shell glue entry points keeps multiple game
frameworks and chained frameworks isolated inside one DmShell process.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Iterable

TAG_RE = re.compile(r'^[0-9A-Za-z_]+$')
IMPL_RE = re.compile(r'@implementation\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s*(\())?')
IFACE_SUPER_RE = re.compile(r'@interface\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s*([:(]))?')
DLSYM_LITERAL_RE = re.compile(r'\bdlsym\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)')
MARKER_MACRO_RE = re.compile(r'^\s*#\s*define\s+DM_FRAMEWORK_MARKER\s+"([A-Za-z_][A-Za-z0-9_]*)"', re.MULTILINE)

SOURCE_SUFFIXES = {'.m', '.mm', '.h', '.inc'}
EXCLUDED_DIR_NAMES = {
    'tests',
    'test',
    'probe',
    'Shell',
    'tools',
    'third_party',
    'games',
    'mac',
    'Gen',
    'build',
}

BASE_GLUE_SYMBOLS = (
    'dmgame_image_marker',
    'dm_glue_version',
    'dm_glue_start_game',
    'dm_glue_present_game',
    'dm_glue_focus_event',
)


def strip_comments(source: str, *, strip_literals: bool = True) -> str:
    """Strip C and Objective-C comments, optionally blanking string and char literal bodies."""
    out: list[str] = []
    n = len(source)
    i = 0
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ''
        if ch == '/' and nxt == '/':
            out.append('  ')
            i += 2
            while i < n and source[i] != '\n':
                out.append(' ')
                i += 1
            continue
        if ch == '/' and nxt == '*':
            out.append('  ')
            i += 2
            while i < n:
                if source[i] == '*' and i + 1 < n and source[i + 1] == '/':
                    out.append('  ')
                    i += 2
                    break
                out.append('\n' if source[i] == '\n' else ' ')
                i += 1
            continue
        if ch == 'R' and nxt == '"' and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] == '_')):
            j = i + 2
            delim_chars: list[str] = []
            valid_raw = False
            while j < n:
                c = source[j]
                if c == '(':
                    valid_raw = True
                    break
                if c in '()\\ \t\r\n' or len(delim_chars) >= 16:
                    break
                delim_chars.append(c)
                j += 1
            if valid_raw:
                delim = ''.join(delim_chars)
                prefix = source[i:j + 1]
                close_seq = f'){delim}"'
                body_start = j + 1
                end_idx = source.find(close_seq, body_start)
                if end_idx != -1:
                    body_end = end_idx
                    after = end_idx + len(close_seq)
                    suffix = close_seq
                else:
                    body_end = n
                    after = n
                    suffix = ''
                body = source[body_start:body_end]
                if strip_literals:
                    out.append(prefix)
                    out.append(''.join('\n' if bc == '\n' else ' ' for bc in body))
                    out.append(suffix)
                else:
                    out.append(source[i:after])
                i = after
                continue
        if ch in ('"', "'"):
            quote = ch
            out.append(quote)
            i += 1
            while i < n:
                c = source[i]
                if c == '\\' and i + 1 < n:
                    if strip_literals:
                        out.append(' ')
                        out.append('\n' if source[i + 1] == '\n' else ' ')
                    else:
                        out.append(c)
                        out.append(source[i + 1])
                    i += 2
                    continue
                if c == quote:
                    out.append(quote)
                    i += 1
                    break
                if strip_literals:
                    out.append('\n' if c == '\n' else ' ')
                else:
                    out.append(c)
                i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def strip_comments_and_literals(source: str) -> str:
    """Replace C and Objective-C comments and string or char literal bodies with spaces."""
    return strip_comments(source, strip_literals=True)


def _should_skip_file(path: Path, rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts[:-1]:
        if part in EXCLUDED_DIR_NAMES or part.startswith('.'):
            return True
    if path.suffix not in SOURCE_SUFFIXES:
        return True
    if path.stem.endswith('_mac'):
        return True
    # Sources/main.m is the single-game app entry point (App, SceneDelegate) and is not linked into DmShellGame.
    if path.name == 'main.m' and path.parent.name == 'Sources':
        return True
    return False


def iter_source_files(roots: Iterable[Path]) -> list[Path]:
    """Collect candidate Objective-C source and header files in deterministic order."""
    seen: set[Path] = set()
    files: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root)
        if not root.exists():
            raise ValueError(f'source path does not exist: {root}')
        resolved_root = root.resolve()
        if resolved_root.is_file():
            if resolved_root.suffix in SOURCE_SUFFIXES and not resolved_root.stem.endswith('_mac'):
                if not (resolved_root.name == 'main.m' and resolved_root.parent.name == 'Sources'):
                    if resolved_root not in seen:
                        seen.add(resolved_root)
                        files.append(resolved_root)
            continue
        for candidate in sorted(root.rglob('*')):
            if not candidate.is_file():
                continue
            rel_parts = candidate.relative_to(root).parts
            if _should_skip_file(candidate, rel_parts):
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    return files


def extract_classes_from_text(source: str) -> tuple[set[str], set[str]]:
    """Return (implemented_classes, declared_subclasses) found in one Objective-C file."""
    cleaned = strip_comments_and_literals(source)
    implemented: set[str] = set()
    for match in IMPL_RE.finditer(cleaned):
        if match.group(2) == '(':
            continue
        implemented.add(match.group(1))
    declared: set[str] = set()
    for match in IFACE_SUPER_RE.finditer(cleaned):
        if match.group(2) == ':':
            declared.add(match.group(1))
    return implemented, declared


def scan_objc_classes(roots: Iterable[Path]) -> list[str]:
    """Scan engine directories or files and return sorted project-owned Objective-C class names."""
    implemented_all: set[str] = set()
    for path in iter_source_files(roots):
        text = path.read_text(encoding='utf-8', errors='replace')
        impl, _ = extract_classes_from_text(text)
        implemented_all.update(impl)
    return sorted(implemented_all)


def make_namespace_tag(
    *,
    tag: str | None = None,
    game: str | None = None,
    framework_name: str | None = None,
    chain_index: str | None = None,
) -> str:
    """Build and validate a safe [0-9A-Za-z_]+ namespace tag for a game or chained framework."""
    if tag is not None and tag != '':
        if not TAG_RE.fullmatch(tag):
            raise ValueError(f'invalid namespace tag {tag!r}: only [0-9A-Za-z_] characters are allowed')
        return tag

    parts: list[str] = []
    base_game = (game or '').strip().upper()
    if base_game:
        if not TAG_RE.fullmatch(base_game):
            raise ValueError(f'invalid game identifier {game!r}: only [0-9A-Za-z_] characters are allowed')
        parts.append(base_game)

    fw = (framework_name or '').strip()
    if fw and fw != 'DmGame':
        if not TAG_RE.fullmatch(fw):
            raise ValueError(f'invalid framework name {framework_name!r}: only [0-9A-Za-z_] characters are allowed')
        expected_prefix = f'DMGAME{base_game}' if base_game else 'DMGAME'
        if fw.upper() == expected_prefix:
            pass
        elif fw.upper().startswith(expected_prefix) and len(fw) > len(expected_prefix):
            suffix = fw[len(expected_prefix):].lstrip('_')
            if suffix:
                parts.append(suffix)
        else:
            parts.append(fw)

    if chain_index is not None and str(chain_index).strip() != '':
        raw_chain = str(chain_index).strip()
        if not TAG_RE.fullmatch(raw_chain):
            raise ValueError(f'invalid chain index {chain_index!r}: only [0-9A-Za-z_] characters are allowed')
        chain_token = f'c{raw_chain}' if raw_chain.isdigit() else raw_chain
        if not parts or parts[-1] != chain_token:
            parts.append(chain_token)

    result = '_'.join(p for p in parts if p)
    if not result or not TAG_RE.fullmatch(result):
        raise ValueError('could not derive a valid namespace tag (pass --tag or --game)')
    return result


def collect_exported_symbols(dm_ios_dir: Path | None = None) -> list[str]:
    """Return Mach-O symbols (with leading underscore) that DmShellGame must export for dlsym."""
    names: list[str] = list(BASE_GLUE_SYMBOLS)
    if dm_ios_dir is not None:
        bootstrap_h = dm_ios_dir / 'Sources' / 'FrameworkBootstrap.h'
        if bootstrap_h.is_file():
            no_comments = strip_comments(
                bootstrap_h.read_text(encoding='utf-8', errors='replace'),
                strip_literals=False,
            )
            for m in MARKER_MACRO_RE.finditer(no_comments):
                if m.group(1) not in names:
                    names.insert(0, m.group(1))
        shell_main = dm_ios_dir / 'Shell' / 'ShellMain.m'
        if shell_main.is_file():
            no_comments = strip_comments(
                shell_main.read_text(encoding='utf-8', errors='replace'),
                strip_literals=False,
            )
            for m in DLSYM_LITERAL_RE.finditer(no_comments):
                sym = m.group(1)
                if sym not in names:
                    names.append(sym)
    return [f'_{name}' for name in names]


def render_namespace_header(tag: str, classes: Iterable[str]) -> str:
    """Render objc_namespace.h mapping each project-owned class to DmFw_<tag>_<Class>."""
    if not TAG_RE.fullmatch(tag):
        raise ValueError(f'invalid namespace tag {tag!r}')
    sorted_classes = sorted(set(classes))
    for cls in sorted_classes:
        if not TAG_RE.fullmatch(cls) or cls[0].isdigit():
            raise ValueError(f'invalid Objective-C class identifier {cls!r}')
    lines = [
        f'// Generated by gen_objc_namespace.py for DmShellGame namespace {tag}',
        '#ifndef DM_OBJC_NAMESPACE_H',
        '#define DM_OBJC_NAMESPACE_H',
        '',
    ]
    for cls in sorted_classes:
        lines.append(f'#ifndef {cls}')
        lines.append(f'#define {cls} DmFw_{tag}_{cls}')
        lines.append('#endif')
    lines.append('')
    lines.append('#endif')
    return '\n'.join(lines) + '\n'


def render_exported_symbols(symbols: Iterable[str]) -> str:
    """Render exported_symbols.txt for ld64 -exported_symbols_list."""
    lines = [sym if sym.startswith('_') else f'_{sym}' for sym in symbols]
    return '\n'.join(dict.fromkeys(lines)) + '\n'


def default_source_roots(dm_dir: Path) -> list[Path]:
    candidates = (
        dm_dir / 'host',
        dm_dir / 'nvn',
        dm_dir / 'gfx',
        dm_dir / 'audio',
        dm_dir / 'ios' / 'Sources',
        dm_dir / 'ios' / 'framework',
    )
    return [p for p in candidates if p.exists()]


def write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode('utf-8')
    if path.is_file():
        try:
            if path.read_bytes() == data:
                return
        except OSError:
            pass
    path.write_bytes(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Generate Objective-C namespace macros and Mach-O export list for DmShellGame.'
    )
    parser.add_argument('--tag', help='Explicit [0-9A-Za-z_]+ namespace tag')
    parser.add_argument('--game', help='Game title ID used to derive the namespace tag')
    parser.add_argument('--framework-name', help='Framework executable name (distinguishes chained frameworks)')
    parser.add_argument('--chain-index', help='Optional chain index appended to the namespace tag')
    parser.add_argument('--header', type=Path, help='Output path for objc_namespace.h')
    parser.add_argument('--exports', type=Path, help='Output path for exported_symbols.txt')
    parser.add_argument('--print-tag', action='store_true', help='Print the resolved namespace tag to stdout')
    parser.add_argument('sources', nargs='*', type=Path, help='Source directories or files to scan')
    args = parser.parse_args(argv)

    dm_ios_dir = Path(__file__).resolve().parents[1]
    dm_dir = dm_ios_dir.parent

    try:
        ns_tag = make_namespace_tag(
            tag=args.tag,
            game=args.game,
            framework_name=args.framework_name,
            chain_index=args.chain_index,
        )
        roots = list(args.sources) if args.sources else default_source_roots(dm_dir)
        classes = scan_objc_classes(roots)
        if not classes:
            raise ValueError('no Objective-C classes found in the scanned source paths')
    except ValueError as exc:
        print(f'gen_objc_namespace.py: {exc}', file=sys.stderr)
        return 1

    if args.header is not None:
        write_if_changed(args.header, render_namespace_header(ns_tag, classes))
    if args.exports is not None:
        symbols = collect_exported_symbols(dm_ios_dir)
        write_if_changed(args.exports, render_exported_symbols(symbols))
    if args.print_tag:
        print(ns_tag)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
