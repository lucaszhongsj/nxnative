#!/usr/bin/env python3
"""Persistent build seed cache (Schema v1) for fast launcher/engine refreshes without NSPs or prod.keys."""

import argparse
import datetime
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TOOLS_DIR, '..', '..'))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dm.tools import nacp, romfs, romfs_image, shader_lib  # noqa: E402

SEED_SCHEMA_VERSION = 1
SEED_MAX_ENTRIES = 500_000
SEED_MAX_BYTES = 32 << 30
COPY_CHUNK = 8 << 20

_TID_RE = re.compile(r'^[0-9A-F]{16}$')
_PROG_RE = re.compile(r'^p(\d+)$')
_VER_RE = re.compile(r'^v(\d+)$')
_STALE_TMP_RE = re.compile(r'^\.([0-9A-F]{16})\.(staging|old)\.(\d+)$')

SEED_FIELDS = (
    'schema',
    'title_id',
    'base_title_id',
    'pkg_ver',
    'app_name',
    'app_version',
    'bundle_id',
    'lang',
    'langs_available',
    'gfx_api',
    'chain',
    'profile_id',
    'profile_kind',
    'is_multi_program',
    'uses_jit',
    'has_aoc',
    'has_html',
    'has_icon',
    'inputs_digest',
    'romfs_identity',
    'updated_at',
    'programs',
)

_OPTIONAL_CAPTURE_DIRS = ('vk_capture', 'gl_capture', 'glslc_capture', 'ue_shaders')
_EMPTY_TABLE_HEADERS = {
    'c1_full.tsv': '# empty table placeholder\n',
    'cb_extent.tsv': '# empty table placeholder\n',
}


class _Quota:
    """Unified entry and byte budget across file staging, hardlinking and shader archive packing."""

    def __init__(self, max_entries=SEED_MAX_ENTRIES, max_bytes=SEED_MAX_BYTES):
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self.used_entries = 0
        self.used_bytes = 0

    def charge(self, entries, nbytes, label=''):
        if entries < 0 or nbytes < 0:
            raise ValueError(f'negative quota charge for {label}')
        if entries > self.max_entries - self.used_entries:
            raise ValueError(f'seed entry quota exceeded ({self.max_entries} max) at {label}')
        if nbytes > self.max_bytes - self.used_bytes:
            raise ValueError(f'seed byte quota exceeded ({self.max_bytes} bytes max) at {label}')
        self.used_entries += entries
        self.used_bytes += nbytes


def default_seed_root():
    """Returns the persistent build seed cache root, honoring DM_SEED_CACHE_DIR when set."""
    override = os.environ.get('DM_SEED_CACHE_DIR', '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser('~')
    if sys.platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support', 'nxnative', 'build-seeds')
    if sys.platform in ('win32', 'cygwin') or os.name == 'nt':
        local_app_data = os.environ.get('LOCALAPPDATA', '').strip()
        if local_app_data:
            return os.path.join(local_app_data, 'nxnative', 'build-seeds')
        return os.path.join(home, '.local', 'share', 'nxnative', 'build-seeds')
    xdg = os.environ.get('XDG_DATA_HOME', '').strip()
    if xdg:
        return os.path.join(xdg, 'nxnative', 'build-seeds')
    return os.path.join(home, '.local', 'share', 'nxnative', 'build-seeds')


def _validate_title_id(title_id):
    if not isinstance(title_id, str):
        raise ValueError(f'invalid title_id: {title_id!r}')
    tid = title_id.strip().upper()
    if not _TID_RE.fullmatch(tid):
        raise ValueError(f'invalid 16-hex title_id: {title_id!r}')
    return tid


def _default_base_title_id(tid):
    return tid[:13] + '000'


def _norm_chain(chain_val):
    """Normalizes 'b-<hex>' or '<hex>' to raw '<hex>' (empty string when absent)."""
    if not chain_val:
        return ''
    s = str(chain_val).strip()
    if s.startswith('b-'):
        s = s[2:]
    return s


def _chain_tag(chain_val):
    """Formats a chain value as 'b-<hex>' (empty string when absent)."""
    raw = _norm_chain(chain_val)
    return f'b-{raw}' if raw else ''


def _validate_rel_path(rel_path, label='path'):
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError(f'empty relative path in {label}')
    if rel_path.startswith(('/', '\\')) or '\\' in rel_path or ':' in rel_path or '\0' in rel_path:
        raise ValueError(f'unsafe relative path {rel_path!r} in {label}')
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in rel_path):
        raise ValueError(f'non-UTF-8 surrogate in path {rel_path!r} in {label}')
    parts = rel_path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'unsafe path segment in {rel_path!r} in {label}')
    return parts


def _pid_alive(pid):
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True


def _sweep_stale_temp_dirs(seed_root, tid=None):
    if not os.path.isdir(seed_root) or os.path.islink(seed_root):
        return
    try:
        entries = list(os.scandir(seed_root))
    except OSError:
        return
    for entry in entries:
        m = _STALE_TMP_RE.fullmatch(entry.name)
        if not m:
            continue
        entry_tid, _kind, pid_str = m.group(1), m.group(2), m.group(3)
        if tid and entry_tid != tid:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if not _pid_alive(pid):
            if entry.is_symlink():
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass
            else:
                shutil.rmtree(entry.path, ignore_errors=True)


def _assert_not_overlapping_dirs(src_dir, dst_dir):
    src_real = os.path.realpath(src_dir)
    dst_parent_real = os.path.realpath(os.path.dirname(os.path.abspath(dst_dir)))
    dst_real = os.path.join(dst_parent_real, os.path.basename(os.path.abspath(dst_dir)))
    if os.path.exists(dst_dir):
        dst_real = os.path.realpath(dst_dir)
    try:
        common = os.path.commonpath([src_real, dst_real])
    except ValueError:
        return
    if common == src_real or common == dst_real:
        raise ValueError(f'refusing recursive directory copy between {src_dir} and {dst_dir}')


def _link_or_copy_file(src, dst, quota):
    src_path = os.fspath(src)
    dst_path = os.fspath(dst)
    st = os.lstat(src_path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f'symlink forbidden in build seed input: {src_path}')
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f'non-regular file forbidden in build seed input: {src_path}')
    quota.charge(1, st.st_size, src_path)

    parent = os.path.dirname(dst_path)
    if os.path.islink(parent):
        raise ValueError(f'symlink parent forbidden: {parent}')
    os.makedirs(parent, exist_ok=True)

    if os.path.lexists(dst_path):
        if os.path.islink(dst_path) or os.path.isdir(dst_path):
            raise ValueError(f'refusing to overwrite directory or symlink: {dst_path}')
        os.unlink(dst_path)

    try:
        os.link(src_path, dst_path)
        return
    except OSError:
        pass

    tmp_dst = f'{dst_path}.part.{os.getpid()}'
    if os.path.islink(tmp_dst):
        raise ValueError(f'symlink temporary path forbidden: {tmp_dst}')
    try:
        with open(src_path, 'rb') as fin, open(tmp_dst, 'wb') as fout:
            while True:
                buf = fin.read(COPY_CHUNK)
                if not buf:
                    break
                fout.write(buf)
        try:
            os.chmod(tmp_dst, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        os.replace(tmp_dst, dst_path)
    except BaseException:
        try:
            os.unlink(tmp_dst)
        except OSError:
            pass
        raise


def _stage_tree(src_dir, dst_dir, quota, allow_subdirs=True, skip_names=(), _ancestors=None):
    src_path = os.fspath(src_dir)
    dst_path = os.fspath(dst_dir)
    st = os.lstat(src_path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f'symlink directory forbidden: {src_path}')
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f'expected directory: {src_path}')
    if _ancestors is None:
        _assert_not_overlapping_dirs(src_path, dst_path)
        _ancestors = set()
    dev_ino = (st.st_dev, st.st_ino)
    if dev_ino in _ancestors:
        raise ValueError(f'recursive directory cycle detected at {src_path}')
    next_ancestors = _ancestors | {dev_ino}

    if os.path.islink(dst_path):
        raise ValueError(f'symlink destination forbidden: {dst_path}')
    os.makedirs(dst_path, exist_ok=True)
    quota.charge(1, 0, src_path)

    with os.scandir(src_path) as it:
        entries = sorted(it, key=lambda e: e.name)

    for entry in entries:
        if entry.is_symlink():
            raise ValueError(f'symlink forbidden inside tree: {entry.path}')
        name = entry.name
        if name in ('.DS_Store',) or '.tmp' in name or name in skip_names:
            continue
        _validate_rel_path(name, src_path)
        target = os.path.join(dst_path, name)
        if entry.is_dir(follow_symlinks=False):
            if not allow_subdirs:
                raise ValueError(f'unexpected subdirectory inside flat folder: {entry.path}')
            _stage_tree(entry.path, target, quota, allow_subdirs=True, skip_names=skip_names, _ancestors=next_ancestors)
        elif entry.is_file(follow_symlinks=False):
            _link_or_copy_file(entry.path, target, quota)
        else:
            raise ValueError(f'unsupported special file in tree: {entry.path}')


def _verify_no_symlinks_or_unsafe_names(root_dir):
    """Walks root_dir without following symlinks and returns (ok, reason, total_bytes)."""
    st = os.lstat(root_dir)
    if stat.S_ISLNK(st.st_mode):
        return False, f'seed path is a symlink: {root_dir}', 0
    if not stat.S_ISDIR(st.st_mode):
        return False, f'seed path is not a directory: {root_dir}', 0
    total_bytes = 0
    total_entries = 0
    stack = [root_dir]
    visited = {(st.st_dev, st.st_ino)}
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError as exc:
            return False, f'cannot scan {cur}: {exc}', total_bytes
        for entry in entries:
            total_entries += 1
            if total_entries > SEED_MAX_ENTRIES:
                return False, f'seed exceeds entry quota ({SEED_MAX_ENTRIES})', total_bytes
            try:
                _validate_rel_path(entry.name, cur)
            except ValueError as exc:
                return False, str(exc), total_bytes
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return False, f'cannot stat {entry.path}: {exc}', total_bytes
            if stat.S_ISLNK(est.st_mode):
                return False, f'symlink forbidden inside seed: {entry.path}', total_bytes
            if stat.S_ISDIR(est.st_mode):
                key = (est.st_dev, est.st_ino)
                if key in visited:
                    return False, f'directory loop inside seed: {entry.path}', total_bytes
                visited.add(key)
                stack.append(entry.path)
            elif stat.S_ISREG(est.st_mode):
                if est.st_size > SEED_MAX_BYTES - total_bytes:
                    return False, f'seed exceeds byte quota ({SEED_MAX_BYTES})', total_bytes
                total_bytes += est.st_size
            else:
                return False, f'special file forbidden inside seed: {entry.path}', total_bytes
    return True, None, total_bytes


def _dir_has_visible_files(path):
    if not os.path.isdir(path) or os.path.islink(path):
        return False
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.name.startswith('.') or '.tmp' in entry.name:
                        continue
                    if entry.is_symlink():
                        return False
                    if entry.is_file(follow_symlinks=False):
                        return True
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            return False
    return False


def _normalize_romfs_identity(val):
    if val is None:
        return None
    if isinstance(val, dict):
        if 'size' not in val or 'index' not in val:
            return None
        title = str(val.get('title', '')).strip()
        ver = str(val.get('version', '')).strip()
        build = str(val.get('build', '')).strip()
        return f"{romfs_image.ID_MAGIC} title={title} version={ver} build={build} size={int(val['size'])} index={val['index']}"
    s = str(val).strip()
    if not s:
        return None
    romfs_image.parse_identity(s)
    return s


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _highest_version_dir(extract_root, allow_patched=False):
    if not extract_root or not os.path.isdir(extract_root) or os.path.islink(extract_root):
        return None, None
    best = None
    try:
        with os.scandir(extract_root) as it:
            for entry in it:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                m = _VER_RE.fullmatch(entry.name)
                if m and os.path.isdir(os.path.join(entry.path, 'ExeFS')):
                    ver_num = int(m.group(1))
                    if best is None or ver_num > best[0]:
                        best = (ver_num, entry.name, entry.path)
    except OSError:
        return None, None
    if best is not None:
        return best[1], best[2]
    if allow_patched:
        try:
            with os.scandir(extract_root) as it:
                for entry in it:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith('patched_v'):
                        m = _VER_RE.fullmatch(entry.name[len('patched_'):])
                        if m:
                            ver_num = int(m.group(1))
                            if best is None or ver_num > best[0]:
                                best = (ver_num, entry.name[len('patched_'):], entry.path)
        except OSError:
            return None, None
        if best is not None:
            return best[1], best[2]
    return None, None


def _pack_metal_sources_one(src_lib_path, dst_img_path, quota, extra_lib_path=None):
    """Packs a single b-<chain> folder or .img into dst_img_path keeping only *.metal, *.json, *.tsv
    and 0-byte placeholders for *_bx.metallib and *_bf.metallib while excluding *_b.metallib payloads."""
    sources = [src_lib_path]
    if extra_lib_path:
        sources.append(extra_lib_path)

    files_map = {}
    for cur_src in sources:
        folder = shader_lib.folder_of(os.fspath(cur_src))
        img_file = folder + '.img'
        if os.path.islink(folder) or os.path.islink(img_file):
            raise ValueError(f'symlink forbidden in Mac shader cache: {cur_src}')
        if os.path.isdir(folder):
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.is_symlink():
                        raise ValueError(f'symlink forbidden in Mac shader folder: {entry.path}')
                    if not entry.name.startswith('.') and '.tmp' not in entry.name:
                        _validate_rel_path(entry.name, folder)

        lib = shader_lib.view(cur_src)
        if not lib.is_dir():
            continue
        for name in sorted(romfs.os.listdir(lib)):
            if name.startswith('.') or '.tmp' in name or name in files_map:
                continue
            _validate_rel_path(name, str(cur_src))
            if '/' in name:
                raise ValueError(f'nested path forbidden inside shader library: {name}')
            if name.endswith(('.metal', '.json', '.tsv')):
                data = (lib / name).read_bytes()
                quota.charge(1, len(data), name)
                files_map[name] = data
            elif name.endswith(('_bx.metallib', '_bf.metallib')):
                quota.charge(1, 0, name)
                files_map[name] = b''
    if not files_map:
        return False

    os.makedirs(os.path.dirname(os.fspath(dst_img_path)), exist_ok=True)
    shader_lib.write(files_map, os.fspath(dst_img_path))
    return True


def _discover_b_libs_in_dir(parent_dir):
    """Returns sorted unique b-<chain> base names present as folders or .img files inside parent_dir."""
    if not parent_dir or not os.path.isdir(parent_dir) or os.path.islink(parent_dir):
        return []
    found = set()
    with os.scandir(parent_dir) as it:
        for entry in it:
            if entry.is_symlink():
                raise ValueError(f'symlink forbidden inside shader directory: {entry.path}')
            if entry.name.startswith('.') or '.tmp' in entry.name:
                continue
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith('b-'):
                found.add(entry.name)
            elif entry.is_file(follow_symlinks=False) and entry.name.startswith('b-') and entry.name.endswith('.img'):
                found.add(entry.name[:-4])
    return sorted(found)


def _stage_metal_sources(mac_shader_dir, work_dir, chain, staging_dir, quota, extra_msl_dir=None):
    """Builds metal-sources/b-<chain>.img (or multi-program p<N>/b-<chain>.img) inside staging_dir.
    Returns inferred raw chain string if discovered."""
    src = os.fspath(mac_shader_dir) if mac_shader_dir else os.path.join(work_dir, 'shader', 'cache')
    if not src:
        return _norm_chain(chain)
    if os.path.islink(src):
        raise ValueError(f'symlink forbidden for mac_shader_dir: {src}')

    target_tag = _chain_tag(chain)
    metal_out_root = os.path.join(staging_dir, 'metal-sources')
    inferred_chain = _norm_chain(chain)

    def _extra_for(b_tag):
        if not extra_msl_dir or not os.path.isdir(extra_msl_dir) or os.path.islink(extra_msl_dir):
            return None
        cand = os.path.join(extra_msl_dir, b_tag)
        if os.path.isdir(cand) or os.path.isfile(cand + '.img'):
            return cand
        return None

    # Case 1: src directly names a shader library (.img file, folder_of+.img, or flat library folder)
    src_folder = shader_lib.folder_of(src)
    src_base = os.path.basename(os.path.normpath(src_folder))
    if os.path.isfile(src) or os.path.isfile(src_folder + '.img') or src_base.startswith('b-'):
        out_tag = src_base if src_base.startswith('b-') else (target_tag or 'b-unknown')
        if not inferred_chain and out_tag.startswith('b-'):
            inferred_chain = _norm_chain(out_tag)
        _pack_metal_sources_one(src, os.path.join(metal_out_root, f'{out_tag}.img'), quota, _extra_for(out_tag))
        return inferred_chain

    if not os.path.isdir(src):
        return inferred_chain

    # Check if src itself is a flat directory directly holding *.metal files (custom test fixture)
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.is_symlink():
            raise ValueError(f'symlink forbidden in {src}: {entry.path}')
    if any(e.is_file(follow_symlinks=False) and e.name.endswith('.metal') for e in entries):
        out_tag = target_tag or 'b-default'
        _pack_metal_sources_one(src, os.path.join(metal_out_root, f'{out_tag}.img'), quota, _extra_for(out_tag))
        return inferred_chain

    # Case 2: multi-program p<N>/ directories under src
    prog_dirs = [e for e in entries if e.is_dir(follow_symlinks=False) and _PROG_RE.fullmatch(e.name)]
    if prog_dirs:
        for pe in prog_dirs:
            b_names = _discover_b_libs_in_dir(pe.path)
            chosen = [target_tag] if (target_tag and target_tag in b_names) else b_names
            for b_name in chosen:
                if not inferred_chain:
                    inferred_chain = _norm_chain(b_name)
                _pack_metal_sources_one(
                    os.path.join(pe.path, b_name),
                    os.path.join(metal_out_root, pe.name, f'{b_name}.img'),
                    quota,
                )
        return inferred_chain

    # Case 3: parent cache directory containing b-<chain> folders and/or b-<chain>.img files
    b_names = set(_discover_b_libs_in_dir(src))
    if extra_msl_dir and os.path.isdir(extra_msl_dir) and not os.path.islink(extra_msl_dir):
        b_names.update(_discover_b_libs_in_dir(extra_msl_dir))
    sorted_b = sorted(b_names)
    chosen = [target_tag] if (target_tag and target_tag in sorted_b) else sorted_b
    for b_name in chosen:
        if not inferred_chain:
            inferred_chain = _norm_chain(b_name)
        _pack_metal_sources_one(
            os.path.join(src, b_name),
            os.path.join(metal_out_root, f'{b_name}.img'),
            quota,
            _extra_for(b_name),
        )
    return inferred_chain


def _stage_shader_ios(ios_src_dir, chain, staging_dir, quota):
    """Stages shader/ios/ into staging_dir (packing loose b-<chain> folders into b-<chain>.img if needed)."""
    dst_ios = os.path.join(staging_dir, 'shader', 'ios')
    os.makedirs(dst_ios, exist_ok=True)
    inferred_chain = _norm_chain(chain)
    if not ios_src_dir or not os.path.exists(ios_src_dir):
        return inferred_chain
    if os.path.islink(ios_src_dir):
        raise ValueError(f'symlink forbidden for shader/ios source: {ios_src_dir}')

    if os.path.isfile(ios_src_dir):
        base = os.path.basename(ios_src_dir)
        _validate_rel_path(base, ios_src_dir)
        if base.startswith('b-') and base.endswith('.img') and not inferred_chain:
            inferred_chain = _norm_chain(base[:-4])
        _link_or_copy_file(ios_src_dir, os.path.join(dst_ios, base), quota)
        return inferred_chain

    with os.scandir(ios_src_dir) as it:
        entries = sorted(it, key=lambda e: e.name)

    b_dirs = {}
    b_imgs = {}
    for entry in entries:
        if entry.is_symlink():
            raise ValueError(f'symlink forbidden inside shader/ios: {entry.path}')
        name = entry.name
        if name in ('.DS_Store',) or '.tmp' in name:
            continue
        _validate_rel_path(name, ios_src_dir)
        if entry.is_dir(follow_symlinks=False) and name.startswith('b-'):
            b_dirs[name] = entry.path
        elif entry.is_file(follow_symlinks=False) and name.startswith('b-') and name.endswith('.img'):
            b_imgs[name[:-4]] = entry.path
        elif entry.is_dir(follow_symlinks=False):
            if _PROG_RE.fullmatch(name) and not inferred_chain:
                sub_b = _discover_b_libs_in_dir(entry.path)
                if sub_b:
                    inferred_chain = _norm_chain(sub_b[0])
            _stage_tree(entry.path, os.path.join(dst_ios, name), quota, allow_subdirs=True)
        elif entry.is_file(follow_symlinks=False):
            _link_or_copy_file(entry.path, os.path.join(dst_ios, name), quota)

    target_tag = _chain_tag(chain)
    all_b = sorted(set(b_dirs) | set(b_imgs))
    chosen_b = [target_tag] if (target_tag and target_tag in all_b) else all_b
    for b_name in chosen_b:
        if not inferred_chain:
            inferred_chain = _norm_chain(b_name)
        if b_name in b_imgs and b_name not in b_dirs:
            _link_or_copy_file(b_imgs[b_name], os.path.join(dst_ios, f'{b_name}.img'), quota)
        else:
            # Merge folder + optional sibling .img into compact b-<chain>.img inside seed
            lib = shader_lib.view(os.path.join(ios_src_dir, b_name))
            files_map = {}
            if b_name in b_dirs:
                with os.scandir(b_dirs[b_name]) as sub_it:
                    for sub_e in sub_it:
                        if sub_e.is_symlink():
                            raise ValueError(f'symlink forbidden in {b_dirs[b_name]}: {sub_e.path}')
                        if not sub_e.name.startswith('.') and '.tmp' not in sub_e.name:
                            _validate_rel_path(sub_e.name, b_dirs[b_name])
            for fname in sorted(romfs.os.listdir(lib)):
                if fname.startswith('.') or '.tmp' in fname:
                    continue
                _validate_rel_path(fname, b_name)
                data = (lib / fname).read_bytes()
                quota.charge(1, len(data), fname)
                files_map[fname] = data
            if files_map:
                shader_lib.write(files_map, os.path.join(dst_ios, f'{b_name}.img'))
    return inferred_chain


def _read_game_tsv_kv(tsv_path):
    kv = {}
    if not tsv_path or not os.path.isfile(tsv_path) or os.path.islink(tsv_path):
        return kv
    try:
        with open(tsv_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return kv
    kv['_header'] = '\n'.join(lines[:5])
    for line in lines:
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t', 1)
        if len(parts) == 2:
            k, v = parts[0].strip(), parts[1].strip()
            if k not in kv:
                kv[k] = v
    return kv


def _find_registered_profile_dir(tid, profile_id=''):
    games_dir = os.path.join(_REPO_ROOT, 'dm', 'games')
    if not os.path.isdir(games_dir):
        return None, None
    if profile_id:
        cand = os.path.join(games_dir, profile_id)
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, 'game.tsv')):
            return profile_id, cand
    try:
        with os.scandir(games_dir) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith('.'):
                    continue
                tsv = os.path.join(entry.path, 'game.tsv')
                kv = _read_game_tsv_kv(tsv)
                if kv.get('title_id', '').strip().upper() == tid:
                    return entry.name, entry.path
    except OSError:
        pass
    return None, None


def _compute_inputs_digest(staging_dir, tid, pkg_ver, gfx_api, chain):
    h = hashlib.sha256()
    h.update(f'{tid}\0{pkg_ver}\0{gfx_api}\0{chain}\0'.encode('utf-8'))
    exefs_dir = os.path.join(staging_dir, 'ExeFS')
    if os.path.isdir(exefs_dir):
        for name in sorted(os.listdir(exefs_dir)):
            p = os.path.join(exefs_dir, name)
            if os.path.isfile(p) and not os.path.islink(p):
                st = os.stat(p)
                h.update(f'exefs:{name}:{st.st_size}\0'.encode('utf-8'))
                with open(p, 'rb') as f:
                    h.update(f.read(65536))
    return h.hexdigest()


def inspect_seed(seed_dir_or_root, title_id=None):
    """Validates a build seed directory and returns a structured status dict."""
    base_arg = os.fspath(seed_dir_or_root)
    expect_tid = None
    if title_id is not None:
        try:
            expect_tid = _validate_title_id(title_id)
        except ValueError as exc:
            return {
                'valid': False,
                'reason': str(exc),
                'seed': None,
                'path': base_arg,
                'size_bytes': 0,
                'has_exefs': False,
                'has_shader_pkg': False,
                'has_shader_ios': False,
                'has_metal_sources': False,
                'has_tables': False,
            }
        cand_sub = os.path.join(base_arg, expect_tid)
        if os.path.lexists(os.path.join(base_arg, 'seed.json')) and not os.path.lexists(cand_sub):
            seed_dir = base_arg
        else:
            seed_dir = cand_sub
    else:
        seed_dir = base_arg

    abs_path = os.path.abspath(seed_dir)
    empty = {
        'valid': False,
        'reason': None,
        'seed': None,
        'path': abs_path,
        'size_bytes': 0,
        'has_exefs': False,
        'has_shader_pkg': False,
        'has_shader_ios': False,
        'has_metal_sources': False,
        'has_tables': False,
    }

    if not os.path.lexists(seed_dir):
        empty['reason'] = f'seed directory does not exist: {abs_path}'
        return empty

    tree_ok, tree_reason, total_bytes = _verify_no_symlinks_or_unsafe_names(seed_dir)
    empty['size_bytes'] = total_bytes
    if not tree_ok:
        empty['reason'] = tree_reason
        return empty

    seed_json_path = os.path.join(seed_dir, 'seed.json')
    if not os.path.isfile(seed_json_path):
        empty['reason'] = f'missing seed.json in {abs_path}'
        return empty

    try:
        with open(seed_json_path, 'r', encoding='utf-8') as f:
            seed = json.load(f)
    except (OSError, ValueError) as exc:
        empty['reason'] = f'invalid seed.json: {exc}'
        return empty

    if not isinstance(seed, dict):
        empty['reason'] = 'seed.json root must be a JSON object'
        return empty
    if seed.get('schema') != SEED_SCHEMA_VERSION:
        empty['reason'] = f'unsupported seed schema: {seed.get("schema")!r}'
        return empty

    try:
        rec_tid = _validate_title_id(seed.get('title_id', ''))
    except ValueError as exc:
        empty['reason'] = str(exc)
        empty['seed'] = seed
        return empty

    if expect_tid and rec_tid != expect_tid:
        empty['reason'] = f'seed title_id {rec_tid} does not match expected {expect_tid}'
        empty['seed'] = seed
        return empty

    dir_base = os.path.basename(abs_path)
    if expect_tid is None and _TID_RE.fullmatch(dir_base.upper()) and dir_base.upper() != rec_tid:
        empty['reason'] = f'seed directory {dir_base} does not match seed title_id {rec_tid}'
        empty['seed'] = seed
        return empty

    exefs_dir = os.path.join(seed_dir, 'ExeFS')
    pkg_dir = os.path.join(seed_dir, 'shader', 'pkg')
    ios_dir = os.path.join(seed_dir, 'shader', 'ios')
    metal_dir = os.path.join(seed_dir, 'metal-sources')
    tables_dir = os.path.join(seed_dir, 'shader', 'tables')

    has_exefs = _dir_has_visible_files(exefs_dir)
    has_shader_pkg = _dir_has_visible_files(pkg_dir)
    has_shader_ios = _dir_has_visible_files(ios_dir)
    has_metal_sources = _dir_has_visible_files(metal_dir)
    has_tables = (
        os.path.isfile(os.path.join(tables_dir, 'c1_full.tsv'))
        and os.path.isfile(os.path.join(tables_dir, 'cb_extent.tsv'))
    )

    gfx_api = str(seed.get('gfx_api') or 'nvn').strip().lower()
    reason = None
    if not has_exefs:
        reason = 'missing or empty ExeFS directory'
    elif not os.path.isdir(pkg_dir):
        reason = 'missing shader/pkg directory'
    elif not os.path.isdir(ios_dir):
        reason = 'missing shader/ios directory'
    elif not has_shader_ios and not has_metal_sources:
        reason = 'seed has neither shader/ios outputs nor metal-sources'
    elif gfx_api == 'nvn' and not has_tables:
        reason = 'missing shader/tables/{c1_full.tsv, cb_extent.tsv}'

    return {
        'valid': reason is None,
        'reason': reason,
        'seed': seed,
        'path': abs_path,
        'size_bytes': total_bytes,
        'has_exefs': has_exefs,
        'has_shader_pkg': has_shader_pkg,
        'has_shader_ios': has_shader_ios,
        'has_metal_sources': has_metal_sources,
        'has_tables': has_tables,
    }


def save_seed(
    seed_root,
    title_id,
    meta,
    work_dir,
    mac_shader_dir=None,
    profile_dir=None,
    exefs_dir=None,
    control_path=None,
    icon_path=None,
    html_dir=None,
    jit_dir=None,
    *,
    _extra_ios_dir=None,
    _extra_tables_dir=None,
):
    """Atomically stages, validates and publishes a Schema v1 build seed with rollback protection."""
    tid = _validate_title_id(title_id)
    root_path = os.fspath(seed_root)
    work_path = os.fspath(work_dir)
    if not root_path or '\0' in root_path:
        raise ValueError('invalid seed_root')
    if os.path.islink(root_path):
        raise ValueError(f'seed_root must not be a symlink: {root_path}')
    if os.path.exists(root_path) and not os.path.isdir(root_path):
        raise ValueError(f'seed_root is not a directory: {root_path}')
    if os.path.islink(work_path):
        raise ValueError(f'work_dir must not be a symlink: {work_path}')

    os.makedirs(root_path, exist_ok=True)
    root_abs = os.path.abspath(root_path)
    _sweep_stale_temp_dirs(root_abs, tid=tid)

    meta_in = dict(meta or {})
    pid = os.getpid()
    staging_dir = os.path.join(root_abs, f'.{tid}.staging.{pid}')
    old_dir = os.path.join(root_abs, f'.{tid}.old.{pid}')
    target_dir = os.path.join(root_abs, tid)

    if os.path.lexists(staging_dir):
        if os.path.islink(staging_dir):
            os.unlink(staging_dir)
        else:
            shutil.rmtree(staging_dir)

    quota = _Quota()
    had_backup = False
    try:
        os.makedirs(staging_dir, exist_ok=False)

        # 1. Determine pkg_ver and version extraction folder
        extract_root = os.path.join(work_path, 'extract')
        disc_ver, disc_ver_dir = _highest_version_dir(extract_root, allow_patched=True)
        pkg_ver = str(meta_in.get('pkg_ver') or disc_ver or 'v0').strip()
        _validate_rel_path(pkg_ver, 'pkg_ver')
        ver_dir = os.path.join(extract_root, pkg_ver)
        if not os.path.isdir(ver_dir) and disc_ver_dir:
            ver_dir = disc_ver_dir
        base_extract_dir = os.path.join(extract_root, 'base')

        # 2. Stage ExeFS/
        eff_exefs = os.fspath(exefs_dir) if exefs_dir else None
        if not eff_exefs:
            for cand in (
                os.path.join(ver_dir, 'ExeFS'),
                os.path.join(work_path, 'ExeFS'),
                os.path.join(base_extract_dir, 'ExeFS'),
            ):
                if os.path.isdir(cand):
                    eff_exefs = cand
                    break
        if not eff_exefs or not os.path.isdir(eff_exefs):
            raise ValueError(f'ExeFS directory not found for {tid} in {work_path}')
        _stage_tree(eff_exefs, os.path.join(staging_dir, 'ExeFS'), quota, allow_subdirs=False)
        if not _dir_has_visible_files(os.path.join(staging_dir, 'ExeFS')):
            raise ValueError(f'ExeFS directory is empty: {eff_exefs}')

        # 3. Stage optional control.nacp, icon.jpg, html/, jit/, and runtime captures
        eff_control = os.fspath(control_path) if control_path else None
        if not eff_control:
            for cand in (
                os.path.join(ver_dir, 'control.nacp'),
                os.path.join(base_extract_dir, 'control.nacp'),
                os.path.join(work_path, 'control.nacp'),
            ):
                if os.path.isfile(cand):
                    eff_control = cand
                    break
        if eff_control and os.path.exists(eff_control):
            _link_or_copy_file(eff_control, os.path.join(staging_dir, 'control.nacp'), quota)

        eff_icon = os.fspath(icon_path) if icon_path else None
        if not eff_icon:
            for cand in (
                os.path.join(ver_dir, 'icon.jpg'),
                os.path.join(base_extract_dir, 'icon.jpg'),
                os.path.join(work_path, 'icon.jpg'),
            ):
                if os.path.isfile(cand):
                    eff_icon = cand
                    break
        has_icon = False
        if eff_icon and os.path.exists(eff_icon):
            _link_or_copy_file(eff_icon, os.path.join(staging_dir, 'icon.jpg'), quota)
            has_icon = True

        eff_html = os.fspath(html_dir) if html_dir else None
        if not eff_html:
            for cand in (
                os.path.join(ver_dir, 'html'),
                os.path.join(base_extract_dir, 'html'),
                os.path.join(work_path, 'html'),
            ):
                if os.path.isdir(cand):
                    eff_html = cand
                    break
        has_html = False
        if eff_html and os.path.exists(eff_html) and _dir_has_visible_files(eff_html):
            _stage_tree(eff_html, os.path.join(staging_dir, 'html'), quota, allow_subdirs=True)
            has_html = True

        eff_jit = os.fspath(jit_dir) if jit_dir else None
        if not eff_jit:
            for cand in (os.path.join(ver_dir, 'jit'), os.path.join(work_path, 'jit')):
                if os.path.isdir(cand):
                    eff_jit = cand
                    break
        has_jit_dir = False
        if eff_jit and os.path.exists(eff_jit) and _dir_has_visible_files(eff_jit):
            _stage_tree(eff_jit, os.path.join(staging_dir, 'jit'), quota, allow_subdirs=True)
            has_jit_dir = True

        for cap_name in _OPTIONAL_CAPTURE_DIRS:
            cap_src = os.path.join(ver_dir, cap_name)
            if os.path.isdir(cap_src) and _dir_has_visible_files(cap_src):
                _stage_tree(cap_src, os.path.join(staging_dir, 'captures', cap_name), quota, allow_subdirs=True)

        # 4. Discover / stage optional derived or provisional profile/<gid>/
        profile_id = str(meta_in.get('profile_id') or '').strip()
        profile_kind = str(meta_in.get('profile_kind') or '').strip()
        eff_profile_dir = os.fspath(profile_dir) if profile_dir else None
        if not eff_profile_dir:
            work_prof_root = os.path.join(work_path, 'profile')
            if os.path.isdir(work_prof_root) and not os.path.islink(work_prof_root):
                with os.scandir(work_prof_root) as it:
                    pdirs = sorted(
                        (e for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith('.')),
                        key=lambda e: e.name,
                    )
                if pdirs:
                    eff_profile_dir = pdirs[0].path
                    if not profile_id:
                        profile_id = pdirs[0].name

        reg_id, reg_dir = _find_registered_profile_dir(tid, profile_id=profile_id)
        prof_kv = {}
        if eff_profile_dir and os.path.exists(eff_profile_dir):
            if os.path.islink(eff_profile_dir):
                raise ValueError(f'symlink forbidden for profile_dir: {eff_profile_dir}')
            prof_kv = _read_game_tsv_kv(os.path.join(eff_profile_dir, 'game.tsv'))
            gid = profile_id or os.path.basename(os.path.normpath(eff_profile_dir))
            _validate_rel_path(gid, 'profile_id')
            profile_id = gid
            is_repo_profile = (
                reg_dir is not None
                and os.path.exists(reg_dir)
                and os.path.realpath(eff_profile_dir) == os.path.realpath(reg_dir)
            )
            if not profile_kind:
                hdr = prof_kv.get('_header', '')
                if 'Provisional game profile' in hdr:
                    profile_kind = 'provisional'
                elif 'Derived ' in hdr:
                    profile_kind = 'derived'
                else:
                    profile_kind = 'registered' if is_repo_profile else 'derived'
            if not is_repo_profile or profile_kind in ('derived', 'provisional'):
                _stage_tree(eff_profile_dir, os.path.join(staging_dir, 'profile', gid), quota, allow_subdirs=True)
                dkey = eff_profile_dir.rstrip('/\\') + '.derived.key'
                if os.path.isfile(dkey) and not os.path.islink(dkey):
                    _link_or_copy_file(dkey, os.path.join(staging_dir, 'profile', f'{gid}.derived.key'), quota)
        elif reg_dir:
            prof_kv = _read_game_tsv_kv(os.path.join(reg_dir, 'game.tsv'))
            if not profile_id:
                profile_id = reg_id
            if not profile_kind:
                profile_kind = 'registered'
        else:
            if not profile_id:
                profile_id = tid.lower()
            if not profile_kind:
                profile_kind = 'registered'

        # 5. Stage shader/pkg/ and sibling stamps
        dst_shader = os.path.join(staging_dir, 'shader')
        dst_pkg = os.path.join(dst_shader, 'pkg')
        os.makedirs(dst_pkg, exist_ok=True)
        src_shader = os.path.join(work_path, 'shader')
        src_pkg = os.path.join(src_shader, 'pkg')
        if os.path.exists(src_pkg):
            _stage_tree(src_pkg, dst_pkg, quota, allow_subdirs=True)
        for stamp_name in ('pkg.key', 'pkg.outputs.key', 'pso_pairs.tsv'):
            sp = os.path.join(src_shader, stamp_name)
            if os.path.exists(sp):
                _link_or_copy_file(sp, os.path.join(dst_shader, stamp_name), quota)

        # Check any existing seed for same pkg_ver so mac-only or Path A refreshes do not drop cached artifacts
        prev_seed = {}
        prev_seed_json = os.path.join(target_dir, 'seed.json')
        if os.path.isfile(prev_seed_json) and not os.path.islink(prev_seed_json):
            try:
                with open(prev_seed_json, 'r', encoding='utf-8') as pf:
                    cand_prev = json.load(pf)
                if isinstance(cand_prev, dict) and str(cand_prev.get('pkg_ver') or '') == pkg_ver:
                    prev_seed = cand_prev
            except (OSError, ValueError):
                prev_seed = {}

        # 6. Stage shader/ios/ and metal-sources/b-<chain>.img
        gfx_api = str(meta_in.get('gfx_api') or prof_kv.get('gfx_api') or 'nvn').strip().lower()
        if gfx_api not in ('nvn', 'vulkan', 'gl'):
            gfx_api = 'nvn'

        raw_chain = _norm_chain(meta_in.get('chain'))
        ios_src = _extra_ios_dir
        if not ios_src:
            cand_ios = os.path.join(src_shader, 'ios')
            cand_legacy = os.path.join(work_path, 'build', 'ios', 'dmsh_b')
            prev_ios = os.path.join(target_dir, 'shader', 'ios')
            if _dir_has_visible_files(cand_ios) and not os.path.islink(cand_ios):
                ios_src = cand_ios
            elif _dir_has_visible_files(cand_legacy) and not os.path.islink(cand_legacy):
                ios_src = cand_legacy
            elif prev_seed and _dir_has_visible_files(prev_ios) and not os.path.islink(prev_ios):
                ios_src = prev_ios

        inferred_ios_chain = _stage_shader_ios(ios_src, raw_chain, staging_dir, quota)
        if not raw_chain and inferred_ios_chain:
            raw_chain = inferred_ios_chain

        if gfx_api == 'nvn':
            inferred_mac_chain = _stage_metal_sources(mac_shader_dir, work_path, raw_chain, staging_dir, quota)
            if not raw_chain and inferred_mac_chain:
                raw_chain = inferred_mac_chain
            staged_metal = os.path.join(staging_dir, 'metal-sources')
            prev_metal = os.path.join(target_dir, 'metal-sources')
            if (
                prev_seed
                and not _dir_has_visible_files(staged_metal)
                and os.path.isdir(prev_metal)
                and not os.path.islink(prev_metal)
                and _dir_has_visible_files(prev_metal)
            ):
                _stage_tree(prev_metal, staged_metal, quota, allow_subdirs=True)
            if not raw_chain:
                hdr_path = os.path.join(_REPO_ROOT, 'dm', 'gfx', 'nvn', 'dmsh_chain.h')
                if os.path.isfile(hdr_path):
                    try:
                        with open(hdr_path, 'r', encoding='utf-8', errors='replace') as hf:
                            m = re.search(r'#define\s+DMSH_CHAIN\s+"([^"]+)"', hf.read())
                            if m:
                                raw_chain = _norm_chain(m.group(1))
                    except OSError:
                        pass
        else:
            raw_chain = ''

        # 7. Stage shader/tables/{c1_full.tsv, cb_extent.tsv} (and optional shader tables)
        dst_tables = os.path.join(dst_shader, 'tables')
        os.makedirs(dst_tables, exist_ok=True)
        table_search_dirs = []
        for tdir in (
            os.path.join(src_shader, 'tables'),
            _extra_tables_dir,
            eff_profile_dir,
            reg_dir,
        ):
            if tdir and os.path.isdir(tdir) and not os.path.islink(tdir):
                table_search_dirs.append(tdir)

        for tname in ('c1_full.tsv', 'cb_extent.tsv', 'shader_map.tsv', 'shader_facts.tsv', 'shader_pairs.tsv', 'pso_pairs.tsv'):
            for tdir in table_search_dirs:
                cand_t = os.path.join(tdir, tname)
                if os.path.isfile(cand_t) and not os.path.islink(cand_t):
                    _link_or_copy_file(cand_t, os.path.join(dst_tables, tname), quota)
                    break
            else:
                if gfx_api == 'nvn' and tname in _EMPTY_TABLE_HEADERS:
                    fallback_path = os.path.join(dst_tables, tname)
                    payload = _EMPTY_TABLE_HEADERS[tname].encode('utf-8')
                    quota.charge(1, len(payload), fallback_path)
                    with open(fallback_path, 'wb') as tf:
                        tf.write(payload)

        # 8. Multi-program sub-builds (work/p1, work/p2, ...) if present
        programs_meta = list(meta_in.get('programs') or [])
        if os.path.isdir(work_path):
            with os.scandir(work_path) as it:
                sub_progs = sorted(
                    (e for e in it if e.is_dir(follow_symlinks=False) and _PROG_RE.fullmatch(e.name) and e.name != 'p0'),
                    key=lambda e: int(_PROG_RE.fullmatch(e.name).group(1)),
                )
            for pe in sub_progs:
                p_ver, p_ver_dir = _highest_version_dir(os.path.join(pe.path, 'extract'), allow_patched=True)
                if p_ver_dir and os.path.isdir(os.path.join(p_ver_dir, 'ExeFS')):
                    p_dst = os.path.join(staging_dir, 'programs', pe.name)
                    _stage_tree(os.path.join(p_ver_dir, 'ExeFS'), os.path.join(p_dst, 'ExeFS'), quota, allow_subdirs=False)
                    if os.path.isdir(os.path.join(pe.path, 'shader', 'pkg')):
                        _stage_tree(os.path.join(pe.path, 'shader', 'pkg'), os.path.join(p_dst, 'shader', 'pkg'), quota)
                    if os.path.isdir(os.path.join(pe.path, 'shader', 'ios')):
                        _stage_shader_ios(os.path.join(pe.path, 'shader', 'ios'), raw_chain, p_dst, quota)
                    if not any(p.get('index') == int(pe.name[1:]) for p in programs_meta if isinstance(p, dict)):
                        programs_meta.append({'index': int(pe.name[1:]), 'pkg_ver': p_ver or 'v0'})

        # 9. Resolve NACP metadata fallbacks and build seed.json
        staged_nacp = os.path.join(staging_dir, 'control.nacp')
        has_nacp = os.path.isfile(staged_nacp)
        nacp_langs = nacp.languages(staged_nacp) if has_nacp else []
        nacp_titles = nacp.names(staged_nacp) if has_nacp else {}
        langs_available = list(meta_in.get('langs_available') or nacp_langs or list(nacp_titles.keys()))
        default_lang = (
            'en-US' if 'en-US' in langs_available
            else ('en' if 'en' in langs_available else (langs_available[0] if langs_available else 'en-US'))
        )
        lang = str(meta_in.get('lang') or default_lang).strip()
        app_name = str(
            meta_in.get('app_name')
            or nacp_titles.get(lang)
            or (nacp.name(staged_nacp) if has_nacp else '')
            or prof_kv.get('name')
            or tid
        ).strip()
        app_version = str(
            meta_in.get('app_version')
            or (nacp.version(staged_nacp) if os.path.isfile(staged_nacp) else '')
            or ''
        ).strip()
        bundle_id = str(meta_in.get('bundle_id') or f'com.nxnative.g{tid.lower()}').strip()
        base_tid_raw = str(meta_in.get('base_title_id') or _default_base_title_id(tid)).strip().upper()
        base_title_id = base_tid_raw if _TID_RE.fullmatch(base_tid_raw) else _default_base_title_id(tid)

        is_multi_program = bool(
            meta_in.get('is_multi_program')
            or bool(programs_meta)
            or bool(prof_kv.get('programs'))
        )
        uses_jit = bool(meta_in.get('uses_jit') or has_jit_dir or prof_kv.get('jit') in ('1', 'true', 'yes'))
        has_aoc = bool(meta_in.get('has_aoc') or os.path.isdir(os.path.join(work_path, 'aoc')))
        has_html = bool(meta_in.get('has_html') or has_html)
        has_icon = bool(meta_in.get('has_icon') or has_icon)

        romfs_id = _normalize_romfs_identity(meta_in.get('romfs_identity'))
        if romfs_id is None:
            for id_cand in (
                os.path.join(ver_dir, 'RomFS.img.id'),
                os.path.join(base_extract_dir, 'RomFS.img.id'),
                os.path.join(work_path, 'romfs.img.id'),
            ):
                if os.path.isfile(id_cand) and not os.path.islink(id_cand):
                    try:
                        with open(id_cand, 'r', encoding='utf-8') as idf:
                            romfs_id = _normalize_romfs_identity(idf.read())
                        if romfs_id:
                            break
                    except (OSError, ValueError):
                        pass
        if romfs_id is None and prev_seed:
            romfs_id = _normalize_romfs_identity(prev_seed.get('romfs_identity'))

        inputs_digest = str(
            meta_in.get('inputs_digest')
            or _compute_inputs_digest(staging_dir, tid, pkg_ver, gfx_api, raw_chain)
        ).strip()

        seed_record = {
            'schema': SEED_SCHEMA_VERSION,
            'title_id': tid,
            'base_title_id': base_title_id,
            'pkg_ver': pkg_ver,
            'app_name': app_name,
            'app_version': app_version,
            'bundle_id': bundle_id,
            'lang': lang,
            'langs_available': langs_available,
            'gfx_api': gfx_api,
            'chain': raw_chain,
            'profile_id': profile_id,
            'profile_kind': profile_kind,
            'is_multi_program': is_multi_program,
            'uses_jit': uses_jit,
            'has_aoc': has_aoc,
            'has_html': has_html,
            'has_icon': has_icon,
            'inputs_digest': inputs_digest,
            'romfs_identity': romfs_id,
            'updated_at': str(meta_in.get('updated_at') or _utc_now_iso()),
            'programs': programs_meta,
        }

        seed_json_bytes = (json.dumps(seed_record, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        quota.charge(1, len(seed_json_bytes), 'seed.json')
        with open(os.path.join(staging_dir, 'seed.json'), 'wb') as sf:
            sf.write(seed_json_bytes)
            sf.flush()
            os.fsync(sf.fileno())

        # 10. Validate staged seed before publishing
        validation = inspect_seed(staging_dir, title_id=tid)
        if not validation['valid']:
            raise ValueError(f'staged seed validation failed: {validation["reason"]}')

        # 11. Atomic swap with unconditional target cleanup + .old rollback on failure
        if os.path.lexists(old_dir):
            if os.path.islink(old_dir) or not os.path.isdir(old_dir):
                os.unlink(old_dir)
            else:
                shutil.rmtree(old_dir)

        if os.path.lexists(target_dir):
            if os.path.islink(target_dir):
                raise ValueError(f'seed target must not be a symlink: {target_dir}')
            os.rename(target_dir, old_dir)
            had_backup = True

        try:
            os.replace(staging_dir, target_dir)
        except BaseException:
            if os.path.islink(target_dir):
                try:
                    os.unlink(target_dir)
                except OSError:
                    pass
            else:
                shutil.rmtree(target_dir, ignore_errors=True)
            if had_backup and os.path.lexists(old_dir):
                try:
                    os.rename(old_dir, target_dir)
                except OSError:
                    shutil.copytree(old_dir, target_dir)
                    shutil.rmtree(old_dir, ignore_errors=True)
            raise

        if had_backup and os.path.lexists(old_dir):
            shutil.rmtree(old_dir, ignore_errors=True)
    finally:
        if os.path.lexists(staging_dir):
            if os.path.islink(staging_dir):
                try:
                    os.unlink(staging_dir)
                except OSError:
                    pass
            else:
                shutil.rmtree(staging_dir, ignore_errors=True)

    return inspect_seed(root_abs, title_id=tid)


def adopt_legacy_work(seed_root, title_id, work_dir, app_path=None, img_path=None):
    """Synthesizes a Schema v1 build seed from a legacy <TID>_build work directory,
    plus an optional Mac .app bundle and/or RomFS .img (reading .id and/or extracting DMMSLTR1 trailer)."""
    tid = _validate_title_id(title_id)
    work_p = os.fspath(work_dir)
    if not os.path.isdir(work_p) or os.path.islink(work_p):
        raise ValueError(f'legacy work_dir does not exist or is a symlink: {work_p}')

    # If caller passed a .app as img_path (e.g. via generic candidate discovery), reclassify cleanly
    if img_path and os.path.isdir(img_path) and str(img_path).endswith('.app') and not app_path:
        app_path, img_path = img_path, None

    meta = {'title_id': tid}
    extra_tables_dir = None
    app_msl_dir = None

    if app_path:
        app_p = os.fspath(app_path)
        if os.path.islink(app_p) or not os.path.isdir(app_p):
            raise ValueError(f'invalid app_path: {app_p}')
        info_plist = os.path.join(app_p, 'Contents', 'Info.plist')
        if os.path.isfile(info_plist) and not os.path.islink(info_plist):
            try:
                with open(info_plist, 'rb') as pf:
                    plist = plistlib.load(pf)
                if isinstance(plist, dict):
                    plist_tid = str(plist.get('DMTitleID') or '').strip().upper()
                    if plist_tid and plist_tid != tid:
                        raise ValueError(f'app DMTitleID {plist_tid} does not match {tid}')
                    if plist.get('CFBundleDisplayName') or plist.get('CFBundleName'):
                        meta.setdefault('app_name', str(plist.get('CFBundleDisplayName') or plist.get('CFBundleName')))
                    if plist.get('DMDisplayVersion'):
                        meta.setdefault('app_version', str(plist['DMDisplayVersion']))
                    if plist.get('DMPackageVersion'):
                        meta.setdefault('pkg_ver', str(plist['DMPackageVersion']))
                    if plist.get('CFBundleIdentifier'):
                        meta.setdefault('bundle_id', str(plist['CFBundleIdentifier']))
                    if plist.get('DMLanguage'):
                        meta.setdefault('lang', str(plist['DMLanguage']))
            except ValueError:
                raise
            except Exception:
                pass
        dm_res = os.path.join(app_p, 'Contents', 'Resources', 'dm')
        if os.path.isdir(dm_res) and not os.path.islink(dm_res):
            extra_tables_dir = dm_res
            lj = os.path.join(dm_res, 'launcher.json')
            if os.path.isfile(lj) and not os.path.islink(lj):
                try:
                    with open(lj, 'r', encoding='utf-8') as lf:
                        ldata = json.load(lf)
                    if isinstance(ldata, dict) and ldata.get('gfx_api'):
                        meta.setdefault('gfx_api', str(ldata['gfx_api']))
                except (OSError, ValueError):
                    pass
        cand_msl = os.path.join(app_p, 'Contents', 'Resources', 'msl')
        if os.path.isdir(cand_msl) and not os.path.islink(cand_msl):
            app_msl_dir = cand_msl
            b_in_app = _discover_b_libs_in_dir(cand_msl)
            if b_in_app:
                meta.setdefault('chain', _norm_chain(b_in_app[-1]))
        if not img_path:
            app_romfs = os.path.join(app_p, 'Contents', 'Resources', 'romfs.img')
            if os.path.isfile(app_romfs) and not os.path.islink(app_romfs):
                img_path = app_romfs

    # Check shell-framework manifest if present inside legacy work_dir
    fw_root = os.path.join(work_p, 'shell-framework')
    if os.path.isdir(fw_root) and not os.path.islink(fw_root):
        try:
            with os.scandir(fw_root) as it:
                fws = sorted((e.path for e in it if e.is_dir(follow_symlinks=False) and e.name.endswith('.framework')))
        except OSError:
            fws = []
        for fw in fws:
            mp = os.path.join(fw, 'game.manifest.json')
            if os.path.isfile(mp) and not os.path.islink(mp):
                try:
                    with open(mp, 'r', encoding='utf-8') as mf:
                        mdata = json.load(mf)
                    if isinstance(mdata, dict):
                        if str(mdata.get('title_id') or '').strip().upper() in ('', tid):
                            if mdata.get('name'):
                                meta.setdefault('app_name', str(mdata['name']))
                            if mdata.get('display_version'):
                                meta.setdefault('app_version', str(mdata['display_version']))
                            if mdata.get('gfx_api'):
                                meta.setdefault('gfx_api', str(mdata['gfx_api']))
                            if mdata.get('chain'):
                                meta['chain'] = _norm_chain(mdata['chain'])
                except (OSError, ValueError):
                    pass

    work_ios_dir = os.path.join(work_p, 'shader', 'ios')
    has_work_ios = _dir_has_visible_files(work_ios_dir) or _dir_has_visible_files(
        os.path.join(work_p, 'build', 'ios', 'dmsh_b')
    )
    mac_cache_dir = os.path.join(work_p, 'shader', 'cache')
    has_mac_cache = _dir_has_visible_files(mac_cache_dir)

    tmp_extract_dir = None
    try:
        if img_path:
            img_p = os.fspath(img_path)
            if os.path.islink(img_p) or not os.path.isfile(img_p):
                raise ValueError(f'invalid img_path: {img_p}')
            id_sidecar = img_p + '.id'
            romfs_id = None
            if os.path.isfile(id_sidecar) and not os.path.islink(id_sidecar):
                try:
                    with open(id_sidecar, 'r', encoding='utf-8') as idf:
                        romfs_id = _normalize_romfs_identity(idf.read())
                except (OSError, ValueError):
                    romfs_id = None
            if not romfs_id:
                romfs_id = romfs_image.identity(img_p, title=tid, version=meta.get('pkg_ver', ''))
            meta['romfs_identity'] = romfs_id
            parsed_id = romfs_image.parse_identity(romfs_id)
            if parsed_id.get('version') and not meta.get('pkg_ver'):
                cand_ver = parsed_id['version']
                if _VER_RE.fullmatch(cand_ver):
                    meta['pkg_ver'] = cand_ver

            t_info = romfs_image.inspect_msl(img_p)
            if t_info['has_trailer']:
                if t_info.get('gfx_api'):
                    meta.setdefault('gfx_api', t_info['gfx_api'])
                if t_info.get('chain'):
                    meta['chain'] = _norm_chain(t_info['chain'])
                if t_info.get('multi_program'):
                    meta['is_multi_program'] = True
                if not has_work_ios:
                    os.makedirs(seed_root, exist_ok=True)
                    tmp_extract_dir = os.path.join(
                        os.path.abspath(seed_root), f'.{tid}.adopt_msl.{os.getpid()}'
                    )
                    if os.path.lexists(tmp_extract_dir):
                        shutil.rmtree(tmp_extract_dir)
                    romfs_image.extract_msl(img_p, tmp_extract_dir)

        eff_mac_shader = mac_cache_dir if has_mac_cache else app_msl_dir
        eff_extra_ios = tmp_extract_dir
        if not has_work_ios and not eff_extra_ios and app_msl_dir and not has_mac_cache:
            # Mac-only legacy build with .app bundle and no iOS shaders yet: keep Mac msl metadata if present
            pass

        return save_seed(
            seed_root=seed_root,
            title_id=tid,
            meta=meta,
            work_dir=work_p,
            mac_shader_dir=eff_mac_shader,
            _extra_ios_dir=eff_extra_ios,
            _extra_tables_dir=extra_tables_dir,
        )
    finally:
        if tmp_extract_dir and os.path.lexists(tmp_extract_dir):
            shutil.rmtree(tmp_extract_dir, ignore_errors=True)


def stage_seed_to_work(seed_dir, work_dir, pkg_ver=None):
    """Stages a validated build seed into work_dir using hardlinks with streaming copy fallback."""
    info = inspect_seed(seed_dir)
    if not info['valid']:
        raise ValueError(f'cannot stage invalid seed {seed_dir}: {info["reason"]}')

    src_seed = info['path']
    seed = info['seed']
    work_p = os.fspath(work_dir)
    if os.path.islink(work_p):
        raise ValueError(f'work_dir must not be a symlink: {work_p}')
    _assert_not_overlapping_dirs(src_seed, work_p)

    eff_ver = str(pkg_ver or seed.get('pkg_ver') or 'v0').strip()
    _validate_rel_path(eff_ver, 'pkg_ver')

    quota = _Quota()
    os.makedirs(work_p, exist_ok=True)
    extract_ver_dir = os.path.join(work_p, 'extract', eff_ver)
    os.makedirs(extract_ver_dir, exist_ok=True)

    # 1. Stage ExeFS/, control.nacp, icon.jpg, html/, jit/ and optional captures into work/extract/<pkg_ver>/
    _stage_tree(os.path.join(src_seed, 'ExeFS'), os.path.join(extract_ver_dir, 'ExeFS'), quota, allow_subdirs=False)

    for single_name in ('control.nacp', 'icon.jpg'):
        sp = os.path.join(src_seed, single_name)
        if os.path.isfile(sp):
            _link_or_copy_file(sp, os.path.join(extract_ver_dir, single_name), quota)

    for opt_dir in ('html', 'jit'):
        sd = os.path.join(src_seed, opt_dir)
        if os.path.isdir(sd):
            _stage_tree(sd, os.path.join(extract_ver_dir, opt_dir), quota, allow_subdirs=True)

    cap_root = os.path.join(src_seed, 'captures')
    if os.path.isdir(cap_root):
        for cap_name in _OPTIONAL_CAPTURE_DIRS:
            cs = os.path.join(cap_root, cap_name)
            if os.path.isdir(cs):
                _stage_tree(cs, os.path.join(extract_ver_dir, cap_name), quota, allow_subdirs=True)

    # 2. Stage shader/pkg, shader/tables, shader/ios and stamps into work/shader/
    work_shader = os.path.join(work_p, 'shader')
    os.makedirs(work_shader, exist_ok=True)

    for sub in ('pkg', 'tables', 'ios'):
        s_sub = os.path.join(src_seed, 'shader', sub)
        if os.path.isdir(s_sub):
            _stage_tree(s_sub, os.path.join(work_shader, sub), quota, allow_subdirs=True)

    for stamp_name in ('pkg.key', 'pkg.outputs.key', 'pso_pairs.tsv'):
        sp = os.path.join(src_seed, 'shader', stamp_name)
        if os.path.isfile(sp):
            _link_or_copy_file(sp, os.path.join(work_shader, stamp_name), quota)

    tables_pso = os.path.join(src_seed, 'shader', 'tables', 'pso_pairs.tsv')
    if os.path.isfile(tables_pso) and not os.path.exists(os.path.join(work_shader, 'pso_pairs.tsv')):
        _link_or_copy_file(tables_pso, os.path.join(work_shader, 'pso_pairs.tsv'), quota)

    # 3. Stage optional derived/provisional profile/<gid>/ into work/profile/<gid>/
    seed_prof = os.path.join(src_seed, 'profile')
    staged_profile_dir = None
    if os.path.isdir(seed_prof):
        work_prof = os.path.join(work_p, 'profile')
        _stage_tree(seed_prof, work_prof, quota, allow_subdirs=True)
        prof_id = str(seed.get('profile_id') or '').strip()
        if prof_id and os.path.isdir(os.path.join(work_prof, prof_id)):
            staged_profile_dir = os.path.join(work_prof, prof_id)
        else:
            with os.scandir(work_prof) as it:
                pdirs = sorted((e.path for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith('.')))
            if pdirs:
                staged_profile_dir = pdirs[0]

        if staged_profile_dir and os.path.isdir(os.path.join(src_seed, 'shader', 'tables')):
            with os.scandir(os.path.join(src_seed, 'shader', 'tables')) as it:
                for te in it:
                    if te.is_file(follow_symlinks=False) and te.name.endswith('.tsv'):
                        dst_t = os.path.join(staged_profile_dir, te.name)
                        if not os.path.exists(dst_t):
                            _link_or_copy_file(te.path, dst_t, quota)

    # 4. Stage multi-program sub-trees if present
    progs_root = os.path.join(src_seed, 'programs')
    if os.path.isdir(progs_root):
        with os.scandir(progs_root) as it:
            for pe in sorted(it, key=lambda e: e.name):
                if pe.is_dir(follow_symlinks=False) and _PROG_RE.fullmatch(pe.name):
                    p_ver = 'v0'
                    for pm in seed.get('programs') or ():
                        if isinstance(pm, dict) and pm.get('index') == int(pe.name[1:]):
                            p_ver = str(pm.get('pkg_ver') or 'v0')
                            break
                    _validate_rel_path(p_ver, 'program pkg_ver')
                    p_work = os.path.join(work_p, pe.name)
                    if os.path.isdir(os.path.join(pe.path, 'ExeFS')):
                        _stage_tree(
                            os.path.join(pe.path, 'ExeFS'),
                            os.path.join(p_work, 'extract', p_ver, 'ExeFS'),
                            quota,
                            allow_subdirs=False,
                        )
                    for sub in ('pkg', 'ios'):
                        if os.path.isdir(os.path.join(pe.path, 'shader', sub)):
                            _stage_tree(
                                os.path.join(pe.path, 'shader', sub),
                                os.path.join(p_work, 'shader', sub),
                                quota,
                            )

    return {
        'seed_dir': src_seed,
        'work_dir': os.path.abspath(work_p),
        'extract_dir': os.path.abspath(extract_ver_dir),
        'pkg_ver': eff_ver,
        'profile_dir': os.path.abspath(staged_profile_dir) if staged_profile_dir else None,
        'seed': seed,
        'staged_files': quota.used_entries,
        'staged_bytes': quota.used_bytes,
    }


def _classify_shader_lib_platform(lib_view, hint=None):
    """Detects whether a shader_lib view holds 'ios' or 'mac' compiled metallibs via JSON chain_metal."""
    for name in sorted(romfs.os.listdir(lib_view)):
        if not name.endswith('.json') or name == '_b_build.json' or name.startswith('.'):
            continue
        try:
            meta = json.loads((lib_view / name).read_text())
        except Exception:
            continue
        if isinstance(meta, dict):
            cm = str(meta.get('chain_metal') or '').lower()
            if 'iphoneos' in cm or 'ios' in cm:
                return 'ios'
            if 'macosx' in cm or 'macos' in cm:
                return 'mac'
    return hint or 'ios'


def _prog_sort_key(prog):
    if not prog:
        return (-1, '')
    m = _PROG_RE.fullmatch(prog)
    if m:
        return (int(m.group(1)), prog)
    return (1 << 30, prog)


def _infer_prog_from_parent(lib_path):
    parent_base = os.path.basename(os.path.dirname(os.path.normpath(os.fspath(lib_path))))
    return parent_base if _PROG_RE.fullmatch(parent_base) else ''


def _discover_prog_b_libs(root_dir):
    """Returns [(prog, b_name, lib_path), ...] for top-level b-* (prog='') and p<N>/b-* subdirectories."""
    if not root_dir or not os.path.isdir(root_dir) or os.path.islink(root_dir):
        return []
    items = []
    for b_name in _discover_b_libs_in_dir(root_dir):
        items.append(('', b_name, os.path.join(root_dir, b_name)))
    prog_entries = []
    with os.scandir(root_dir) as it:
        for entry in it:
            if entry.is_symlink():
                raise ValueError(f'symlink forbidden inside shader directory: {entry.path}')
            if entry.is_dir(follow_symlinks=False) and _PROG_RE.fullmatch(entry.name):
                prog_entries.append((entry.name, entry.path))
    prog_entries.sort(key=lambda item: _prog_sort_key(item[0]))
    for prog_name, prog_path in prog_entries:
        for b_name in _discover_b_libs_in_dir(prog_path):
            items.append((prog_name, b_name, os.path.join(prog_path, b_name)))
    return items


def prepare_prefill(seed_dir, prefill_sources, tmp_dir, gfx_api='nvn', target_chain=None):
    """Prepares DMSH_PREFILL staging directories in tmp_dir from seed metal-sources and prefill_sources.
    Returns (dms_prefill_env_value, summary_dict)."""
    eff_api = str(gfx_api or 'nvn').strip().lower()
    tmp_abs = os.path.abspath(os.fspath(tmp_dir))
    if os.path.islink(tmp_dir):
        raise ValueError(f'tmp_dir must not be a symlink: {tmp_dir}')
    if eff_api != 'nvn':
        return '', {
            'gfx_api': eff_api,
            'chains': [],
            'prefill_mac': None,
            'prefill_ios': None,
            'sources_used': [],
            'dms_prefill': '',
        }

    seed_p = os.path.abspath(os.fspath(seed_dir))
    if os.path.islink(seed_dir):
        raise ValueError(f'seed_dir must not be a symlink: {seed_dir}')

    os.makedirs(tmp_abs, exist_ok=True)
    quota = _Quota()
    norm_target = _norm_chain(target_chain)

    metal_src_libs = {}   # (prog, raw_chain) -> path
    mac_bin_libs = {}     # (prog, raw_chain) -> path
    ios_bin_libs = {}     # (prog, raw_chain) -> path
    sources_used = []

    # 1. Seed's own metal-sources/b-<old>.img and metal-sources/p<N>/b-<old>.img
    seed_metal_dir = os.path.join(seed_p, 'metal-sources')
    for prog, b_name, lib_path in _discover_prog_b_libs(seed_metal_dir):
        ch = _norm_chain(b_name)
        if ch:
            metal_src_libs[(prog, ch)] = lib_path

    extracted_tmp_dirs = []

    def register_lib(lib_path, prog='', chain_hint='', platform_hint=None, origin=''):
        lib_v = shader_lib.view(lib_path)
        if not lib_v.is_dir():
            return
        names = [n for n in romfs.os.listdir(lib_v) if not n.startswith('.') and '.tmp' not in n]
        if not names:
            return
        ch = _norm_chain(chain_hint or shader_lib.name_of(lib_path))
        if not ch:
            stamp = shader_lib.read_stamp(lib_v)
            ch = _norm_chain(stamp.get('chain', ''))
        if not ch:
            return
        key = (prog, ch)
        has_metal = any(n.endswith('.metal') for n in names)
        has_real_metallib = any(
            n.endswith('.metallib') and not n.endswith(('_bx.metallib', '_bf.metallib'))
            for n in names
        )
        if has_metal and key not in metal_src_libs:
            metal_src_libs[key] = os.fspath(lib_path)
        if has_real_metallib:
            plat = _classify_shader_lib_platform(lib_v, hint=platform_hint)
            if plat == 'mac':
                mac_bin_libs.setdefault(key, os.fspath(lib_path))
            else:
                ios_bin_libs.setdefault(key, os.fspath(lib_path))
            if origin and origin not in sources_used:
                sources_used.append(origin)

    def scan_msl_container(container_dir, platform_hint, origin):
        for prog, b_name, lib_path in _discover_prog_b_libs(container_dir):
            register_lib(lib_path, prog=prog, chain_hint=b_name, platform_hint=platform_hint, origin=origin)

    # 2. Inspect external prefill_sources first (.app, RomFS .img with DMMSLTR1 trailer, msl/ dir, b-<chain>[.img])
    for idx, raw_src in enumerate(prefill_sources or ()):
        if not raw_src:
            continue
        src = os.fspath(raw_src)
        if not os.path.exists(src) or os.path.islink(src):
            continue

        if os.path.isdir(src):
            norm_s = os.path.normpath(src)
            base_s = os.path.basename(norm_s)
            if base_s.endswith('.app') or os.path.isdir(os.path.join(norm_s, 'Contents', 'Resources', 'msl')):
                scan_msl_container(os.path.join(norm_s, 'Contents', 'Resources', 'msl'), 'mac', norm_s)
            elif base_s.startswith('b-'):
                hint = 'ios' if '/ios' in norm_s.replace('\\', '/') else None
                register_lib(
                    norm_s,
                    prog=_infer_prog_from_parent(norm_s),
                    chain_hint=base_s,
                    platform_hint=hint,
                    origin=norm_s,
                )
            else:
                hint = 'mac' if 'cache' in base_s else ('ios' if 'ios' in base_s else None)
                scan_msl_container(norm_s, hint, norm_s)
        elif os.path.isfile(src):
            base_s = os.path.basename(src)
            trailer_extracted = False
            if not base_s.startswith('b-'):
                try:
                    t_info = romfs_image.inspect_msl(src)
                except ValueError:
                    t_info = None
                if t_info and t_info.get('has_trailer'):
                    ext_dir = os.path.join(tmp_abs, f'_trailer_{idx}')
                    extracted_tmp_dirs.append(ext_dir)
                    romfs_image.extract_msl(src, ext_dir)
                    scan_msl_container(ext_dir, 'ios', src)
                    trailer_extracted = True
            if not trailer_extracted:
                stem = base_s[:-4] if base_s.endswith('.img') else base_s
                if stem.startswith('b-'):
                    hint = 'mac' if '.app/' in src.replace('\\', '/') else None
                    register_lib(
                        src,
                        prog=_infer_prog_from_parent(src),
                        chain_hint=stem,
                        platform_hint=hint,
                        origin=src,
                    )
                else:
                    try:
                        t_info = romfs_image.inspect_msl(src)
                    except ValueError:
                        t_info = None
                    if t_info and t_info.get('has_trailer'):
                        ext_dir = os.path.join(tmp_abs, f'_trailer_{idx}')
                        extracted_tmp_dirs.append(ext_dir)
                        romfs_image.extract_msl(src, ext_dir)
                        scan_msl_container(ext_dir, 'ios', src)

    # Fallback to seed's own shader/ios/b-<old>.img and shader/ios/p<N>/b-<old>.img
    seed_ios_dir = os.path.join(seed_p, 'shader', 'ios')
    for prog, b_name, lib_path in _discover_prog_b_libs(seed_ios_dir):
        ch = _norm_chain(b_name)
        key = (prog, ch)
        if ch and key not in ios_bin_libs:
            ios_bin_libs[key] = lib_path
            if seed_ios_dir not in sources_used:
                sources_used.append(seed_ios_dir)

    # 3. Pair metal-sources with Mac and/or iOS metallibs per program group
    def _lookup_bin(bin_map, prog, ch):
        exact = bin_map.get((prog, ch))
        if exact is not None:
            return exact
        if prog:
            return bin_map.get(('', ch))
        return bin_map.get(('p0', ch))

    progs_present = sorted({prog for prog, _ch in metal_src_libs}, key=_prog_sort_key)
    paired_chains = []
    seen_chains = set()
    env_specs = []
    first_mac_root = None
    first_ios_root = None

    for prog in progs_present:
        mac_root = os.path.join(tmp_abs, f'prefill_mac_{prog}' if prog else 'prefill_mac')
        ios_root = os.path.join(tmp_abs, f'prefill_ios_{prog}' if prog else 'prefill_ios')
        prog_chains = sorted(ch for p, ch in metal_src_libs if p == prog)
        group_paired = False
        group_has_ios = False

        for ch in prog_chains:
            if norm_target and ch == norm_target:
                continue
            ios_src_path = _lookup_bin(ios_bin_libs, prog, ch)
            mac_src_path = _lookup_bin(mac_bin_libs, prog, ch)
            if ios_src_path is None and mac_src_path is None:
                continue

            b_tag = f'b-{ch}'
            src_metal_view = shader_lib.view(metal_src_libs[(prog, ch)])
            mac_map = {}
            for name in sorted(romfs.os.listdir(src_metal_view)):
                if name.startswith('.') or '.tmp' in name:
                    continue
                _validate_rel_path(name, b_tag)
                data = (src_metal_view / name).read_bytes()
                quota.charge(1, len(data), name)
                mac_map[name] = data

            if mac_src_path is not None:
                mac_bin_view = shader_lib.view(mac_src_path)
                for name in sorted(romfs.os.listdir(mac_bin_view)):
                    if name.startswith('.') or '.tmp' in name:
                        continue
                    _validate_rel_path(name, b_tag)
                    if name.endswith(('.metallib', '.json', '.tsv')):
                        data = (mac_bin_view / name).read_bytes()
                        quota.charge(1, len(data), name)
                        mac_map[name] = data

            os.makedirs(mac_root, exist_ok=True)
            shader_lib.write(mac_map, os.path.join(mac_root, f'{b_tag}.img'))
            group_paired = True

            if ios_src_path is not None:
                ios_dst = os.path.join(ios_root, b_tag)
                os.makedirs(ios_dst, exist_ok=True)
                ios_folder = shader_lib.folder_of(ios_src_path)
                if os.path.isdir(ios_folder) and not os.path.isfile(ios_folder + '.img'):
                    with os.scandir(ios_folder) as it:
                        for entry in sorted(it, key=lambda e: e.name):
                            if entry.is_symlink():
                                raise ValueError(f'symlink forbidden in {ios_folder}: {entry.path}')
                            if entry.name.startswith('.') or '.tmp' in entry.name:
                                continue
                            if entry.is_file(follow_symlinks=False) and entry.name.endswith(('.metallib', '.json', '.tsv')):
                                _validate_rel_path(entry.name, ios_folder)
                                _link_or_copy_file(entry.path, os.path.join(ios_dst, entry.name), quota)
                else:
                    ios_view = shader_lib.view(ios_src_path)
                    for name in sorted(romfs.os.listdir(ios_view)):
                        if name.startswith('.') or '.tmp' in name:
                            continue
                        if name.endswith(('.metallib', '.json', '.tsv')):
                            _validate_rel_path(name, ios_src_path)
                            data = (ios_view / name).read_bytes()
                            quota.charge(1, len(data), name)
                            out_f = os.path.join(ios_dst, name)
                            with open(out_f, 'wb') as f:
                                f.write(data)
                group_has_ios = True

            if b_tag not in seen_chains:
                seen_chains.add(b_tag)
                paired_chains.append(b_tag)

        if group_paired:
            if first_mac_root is None:
                first_mac_root = mac_root
            if group_has_ios and first_ios_root is None:
                first_ios_root = ios_root
            env_specs.append(f'{mac_root}={ios_root}' if group_has_ios else mac_root)

    for ext_d in extracted_tmp_dirs:
        shutil.rmtree(ext_d, ignore_errors=True)

    if not env_specs:
        return '', {
            'gfx_api': eff_api,
            'chains': [],
            'prefill_mac': None,
            'prefill_ios': None,
            'sources_used': sources_used,
            'dms_prefill': '',
        }

    env_val = os.pathsep.join(env_specs)
    return env_val, {
        'gfx_api': eff_api,
        'chains': paired_chains,
        'prefill_mac': first_mac_root,
        'prefill_ios': first_ios_root,
        'sources_used': sources_used,
        'dms_prefill': env_val,
    }


def _build_cli_parser():
    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='print structured JSON output')

    ap = argparse.ArgumentParser(
        description='Manage persistent NX2Apple / nxnative build seeds (Schema v1)',
    )
    ap.add_argument('--json', action='store_true', default=False, help='print structured JSON output')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_root = sub.add_parser('default-root', parents=[common_sub], help='print the default build seed root folder')
    p_root.set_defaults(cmd='default-root')

    p_ins = sub.add_parser('inspect', parents=[common_sub], help='inspect and validate a build seed')
    p_ins.add_argument('seed_path', nargs='?', default=None, help='seed directory or seed root')
    p_ins.add_argument('--seed-cache-dir', '--seed-root', dest='seed_root', default=None, help='seed cache root directory')
    p_ins.add_argument('--title-id', dest='title_id', default=None, help='16-digit hex title ID')

    p_adopt = sub.add_parser('adopt', parents=[common_sub], help='adopt a legacy <TID>_build folder into a persistent seed')
    p_adopt.add_argument('--seed-cache-dir', '--seed-root', dest='seed_root', default=None, help='seed cache root directory')
    p_adopt.add_argument('--title-id', required=True, dest='title_id', help='16-digit hex title ID')
    p_adopt.add_argument('--work-dir', '--work', required=True, dest='work_dir', help='legacy <TID>_build directory')
    p_adopt.add_argument('--app-path', '--app', dest='app_path', default=None, help='optional existing Mac .app bundle')
    p_adopt.add_argument(
        '--img-path', '--img', '--romfs-from', dest='img_path', default=None,
        help='optional RomFS .img file (with optional .id sidecar and/or DMMSLTR1 shader trailer)',
    )

    p_stage = sub.add_parser('stage', parents=[common_sub], help='stage a build seed into a work directory')
    p_stage.add_argument('--seed-dir', required=True, dest='seed_dir', help='path to <seed_root>/<TID>')
    p_stage.add_argument('--work-dir', '--work', required=True, dest='work_dir', help='destination work directory')
    p_stage.add_argument('--pkg-ver', dest='pkg_ver', default=None, help='override extraction version tag')

    p_pre = sub.add_parser('prepare-prefill', parents=[common_sub], help='stage DMSH_PREFILL inputs for toolchain upgrades')
    p_pre.add_argument('--seed-dir', required=True, dest='seed_dir', help='path to <seed_root>/<TID>')
    p_pre.add_argument('--tmp-dir', required=True, dest='tmp_dir', help='temporary directory for prefill_mac and prefill_ios')
    p_pre.add_argument('--source', '--prefill-msl', action='append', default=[], dest='sources', help='prefill source (.app, .img, msl dir, or b-<chain>)')
    p_pre.add_argument('--gfx-api', default='nvn', dest='gfx_api', help='graphics backend (default: nvn)')
    p_pre.add_argument('--target-chain', default=None, dest='target_chain', help='current target DMSH_CHAIN to exclude from old chains')

    return ap


def main(argv=None):
    ap = _build_cli_parser()
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'default-root':
            root = default_seed_root()
            if args.json:
                print(json.dumps({'seed_root': root}, indent=2))
            else:
                print(root)
            return 0

        if args.cmd == 'inspect':
            target = args.seed_path or args.seed_root or default_seed_root()
            res = inspect_seed(target, title_id=args.title_id)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                state = 'valid' if res['valid'] else f"invalid ({res['reason']})"
                print(f"{res['path']}: {state}")
            return 0 if res['valid'] else 1

        if args.cmd == 'adopt':
            root = args.seed_root or default_seed_root()
            res = adopt_legacy_work(
                seed_root=root,
                title_id=args.title_id,
                work_dir=args.work_dir,
                app_path=args.app_path,
                img_path=args.img_path,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"adopted {res['seed']['title_id']} -> {res['path']} (chain {res['seed']['chain'] or '-'})")
            return 0

        if args.cmd == 'stage':
            res = stage_seed_to_work(args.seed_dir, args.work_dir, pkg_ver=args.pkg_ver)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"staged {res['seed_dir']} -> {res['work_dir']} ({res['staged_files']} entries)")
            return 0

        if args.cmd == 'prepare-prefill':
            env_val, summary = prepare_prefill(
                seed_dir=args.seed_dir,
                prefill_sources=args.sources,
                tmp_dir=args.tmp_dir,
                gfx_api=args.gfx_api,
                target_chain=args.target_chain,
            )
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                print(env_val)
            return 0
    except (OSError, ValueError, TypeError) as exc:
        if getattr(args, 'json', False):
            print(json.dumps({'valid': False, 'ok': False, 'error': str(exc)}, indent=2))
            return 1
        sys.stderr.write(f'[RED] {exc}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
