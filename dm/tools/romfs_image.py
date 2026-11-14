#!/usr/bin/env python3
# Pack an extracted RomFS folder into one image file the host reads in place of the folder (hle_fs.c, "RomFS image"): DM_ROMFS may name the image instead of a folder.
# One file instead of hundreds of thousands copies to an iPhone at full speed (Tears of the Kingdom's 304,000 files ran the phone's file service out of open files), and the host
# keeps one descriptor open for the whole RomFS.
# Layout (little-endian):
#   header  32 bytes: magic "DMROMFS1", u64 index offset, u64 index size, u64 entry count
#   data    each file's bytes, one after another
#   index   entry count entries of 24 bytes: u64 a, u64 b, u32 name offset, u16 name length, u8 type (0 directory, 1 file), u8 flags
#           a directory: a = its first child's entry, b = its number of children; a file: a = data offset in the image, b = size
#           flags: 0, or 1 on a file that failed the NCA's hash check and was written as read (nx_extract --allow-damaged; the host names it in its log)
#           then the names, one pool. Entry 0 is the root (empty name). Each directory's children are consecutive entries sorted by name (bytes), so a lookup is a
#           binary search per path component; directories are numbered breadth-first.
# The index comes last, so a copy cut off partway has no valid index (the header points past the end of the file) and the host refuses it.
# Names are the guest names: a host name chosen on a case collision (.dm-case-N) is replaced by the one in its xattr, as hle_fs.c's guest_name does.
# Usage: romfs_image.py [--reuse] <RomFS folder> <out image>   (written to <out>.part, renamed at the end)
# --reuse checks the source tree's names and file metadata, the packer's version and the image's identity before keeping a previous image.
import argparse, hashlib, json, os, re, struct, subprocess
from pathlib import Path

MAGIC = b'DMROMFS1'
HEAD = struct.Struct('<8sQQQ')
ENT = struct.Struct('<QQIHBB')
FOOTER = struct.Struct('<8sIBBHQQQ24s')
MSL_MAGIC = b'DMMSLTR1'
MSL_VERSION = 1
MSL_ALIGN = 16384
MSL_DATA_ALIGN = 16
MSL_FOOTER_ALIGN = 8
MSL_FLAG_MULTI_PROGRAM = 1
GFX_API_CODES = {'nvn': 1, 'vulkan': 2, 'gl': 3}
GFX_API_NAMES = {1: 'nvn', 2: 'vulkan', 3: 'gl'}
MSL_APP_SUFFIXES = ('.metallib', '.json', '.tsv')
MSL_APP_LEAVE_OUT = ('_b_build.json',)
MSL_EXTRACT_MAX_ENTRIES = 200_000
MSL_EXTRACT_MAX_BYTES = 16 << 30
MSL_EXTRACT_CHUNK = 8 << 20
_PROG_DIR_RE = re.compile(r'^p\d+$')
_U64_MAX = (1 << 64) - 1
_I64_MAX = (1 << 63) - 1
GUEST_NAME = 'org.d2rmobile.guest-name'
# Files a dump tool leaves in every folder of the game's tree, which are not game files: an FTP dump keeps the server's listing as .listing (as the NUP spec's §6.3 says a producer
# may). Packed, a game that lists a folder sees one more entry: Nintendo Switch Sports Resort cuts each name at its first '.', loaded "Parameter/<folder>/" with an empty name, got
# no resource and crashed at main+0x9d7c60. A folder's artifacts are left out of the image (and out of a NUP's, pack_nup.py), as they are of the survey's file list
ARTIFACTS = {b'.listing'}


def guest(parent, name):
    if not name.startswith('.dm-case-'): return name
    r = subprocess.run(['xattr', '-p', GUEST_NAME, os.path.join(parent, name)], capture_output=True, text=True)
    return r.stdout.rstrip('\n') if r.returncode == 0 else name


def children(path):
    out = []
    with os.scandir(path) as it:
        for e in it:
            name, isdir = guest(path, e.name).encode(), e.is_dir(follow_symlinks=True)
            if isdir or name not in ARTIFACTS: out.append((name, e.path, isdir))
    out.sort()
    return out


def entries(src):
    ents = [[b'', src, True, 0, 0]]   # name, host path, is dir, a, b
    i = 0
    while i < len(ents):   # breadth-first: each directory's children land as one consecutive run
        name, path, isdir, _, _ = ents[i]
        if isdir:
            kids = children(path)
            ents[i][3], ents[i][4] = len(ents), len(kids)
            ents.extend([n, p, d, 0, 0] for n, p, d in kids)
        i += 1
    return ents


def file_identity(path):   # not st_dev: macOS numbers a volume when it mounts it, so a reboot moved the Data volume from 16777234 to 16777232 and a 24 GB image was packed again, 8 minutes over SMB
    s = os.stat(path)
    return [s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def source_digest(src, ents):
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(os.fsencode(os.path.realpath(src)) + b'\0')
    for name, path, isdir, _, _ in ents:
        h.update(struct.pack('<I?', len(name), isdir)); h.update(name)
        h.update(json.dumps(file_identity(path)).encode() + b'\0')
    return h.hexdigest()


def reusable(dst, digest):   # whether dst is the image the stamp beside it says was made from inputs with this digest
    try: return json.loads(Path(dst + '.inputs.json').read_text()) == {'source': digest, 'image': file_identity(dst)}
    except (OSError, ValueError): return False


def layout(ents, end):
    """The header and the index (entries, then the name pool) of an image of ents whose files' a and b are filled in, the file data ending at end.
    write() writes these bytes and a check of an existing image compares them, so the two cannot disagree on the format."""
    pool, idx = bytearray(), bytearray()
    for name, _, isdir, a, b in ents:
        idx += ENT.pack(a, b, len(pool), len(name), 0 if isdir else 1, 0); pool += name
    idx += pool
    return HEAD.pack(MAGIC, end, len(idx), len(ents)), bytes(idx)


def write(ents, dst, copy):
    """ents (entries(): [name, source, is dir, a, b], breadth-first) → dst + '.part'; copy(source, out) writes one file's bytes to out and returns how many. Fills in each file's a and b"""
    part = dst + '.part'
    with open(part, 'wb') as out:
        out.write(b'\0' * HEAD.size)
        off = HEAD.size
        for e in ents:
            if e[2]: continue
            e[3], e[4] = off, copy(e[1], out); off += e[4]
        head, idx = layout(ents, off)
        out.write(idx)
        out.seek(0); out.write(head)
    return part


def stamp(dst, digest, image):   # the stamp reusable() checks: dst, with identity image, is the image of inputs with this digest
    Path(dst + '.inputs.json.part').write_text(json.dumps({'source': digest, 'image': image}))
    os.replace(dst + '.inputs.json.part', dst + '.inputs.json')


def publish(part, dst, digest):   # the written image in place, with its stamp
    os.replace(part, dst)
    stamp(dst, digest, file_identity(dst))


def copy_file(path, out):
    n = 0
    with open(path, 'rb') as f:
        while b := f.read(8 << 20): out.write(b); n += len(b)
    return n


def pack(src, dst, reuse=False):
    src, dst = os.fspath(src), os.fspath(dst)
    ents = entries(src)
    digest = source_digest(src, ents)
    if reuse and reusable(dst, digest): return len(ents)
    part = write(ents, dst, copy_file)
    # A changing source must not publish a mixed image or a reuse stamp for stale data.
    if source_digest(src, entries(src)) != digest:
        os.unlink(part)
        raise RuntimeError('RomFS changed while packing; run the packer again when its files are stable')
    publish(part, dst, digest)
    return len(ents)


def read_index(img):   # for tests and checks: {guest path: (is dir, offset, size)}
    with open(img, 'rb') as f:
        magic, ioff, isz, n = HEAD.unpack(f.read(HEAD.size))
        assert magic == MAGIC
        f.seek(ioff); raw = f.read(isz)
    pool = raw[n * ENT.size:]
    e = [ENT.unpack_from(raw, k * ENT.size) for k in range(n)]
    out, todo = {}, [(0, '')]
    while todo:
        k, path = todo.pop()
        a, b, no, nl, t, _ = e[k]
        if t == 0:
            if path: out[path] = (True, 0, 0)
            todo.extend((c, (path + '/' if path else '') + pool[e[c][2]:e[c][2] + e[c][3]].decode()) for c in range(a, a + b))
        else: out[path] = (False, a, b)
    return out


# File names that promise a format, and the first bytes that format starts with. A file whose name promises one and whose bytes start with none of its heads is damaged:
# a NUP or a dump folder carries no NCA hashes, so this is the check its extraction has. A zstd stream starts with a frame (28 b5 2f fd) or a skippable frame (5? 2a 4d 18).
# Found on a NUP of Ocarina of Time whose three .zs textures began with 12,288 zero bytes: the game asked for one again on every pass of its background loader (PR #1530).
FORMAT_HEADS = {'.zs': ((b'\x28\xb5\x2f\xfd', b'\xff\xff\xff\xff'), (b'\x50\x2a\x4d\x18', b'\xf0\xff\xff\xff'))}   # suffix: ((bytes, mask), ...)


def format_mismatches(img, progress=None):   # [(guest path, size, first 4 bytes)] of the image's files whose bytes do not start as their name promises (FORMAT_HEADS); empty files are left out. progress(done, total): every 4,096 files
    files = sorted((off, size, p) for p, (d, off, size) in read_index(img).items() if not d and size and os.path.splitext(p)[1].lower() in FORMAT_HEADS)
    bad = []
    with open(img, 'rb') as f:
        for k, (off, size, p) in enumerate(files):   # in image order: one pass forward over the disk
            if progress and not k % 4096: progress(k, len(files))
            f.seek(off); head = f.read(min(size, 4))   # not into the next file's bytes
            if not any(len(head) == 4 and all(h & m == w & m for h, w, m in zip(head, want, mask)) for want, mask in FORMAT_HEADS[os.path.splitext(p)[1].lower()]):
                bad.append((p, size, head))
    return bad


ID_MAGIC = 'DMROMFSID1'
_FNV_OFFSET, _FNV_PRIME, _MASK = 0xcbf29ce484222325, 0x100000001b3, (1 << 64) - 1


def _clean(text):   # one token of the identity line: no spaces, no newlines
    return ''.join(c if c.isalnum() or c in '._-+' else '_' for c in str(text)) or '-'


def check_bounds_and_trailer(f, file_size, ioff, isz, img):
    """Validates [0, ioff + isz) and any optional DMMSLTR1 shader trailer in [ioff + isz, file_size).
    Returns None when file_size == ioff + isz, or a parsed trailer dict when a valid trailer is present.
    Raises ValueError on any overflow, truncation or malformed trailer."""
    if file_size > _I64_MAX or ioff < HEAD.size or ioff > file_size or isz > file_size - ioff:
        raise ValueError(f'incomplete or invalid RomFS image: {img}')
    base_end = ioff + isz
    if file_size == base_end:
        return None
    if file_size - base_end < FOOTER.size:
        raise ValueError(f'incomplete or invalid RomFS trailer: {img}')
    footer_off = file_size - FOOTER.size
    f.seek(footer_off)
    raw = f.read(FOOTER.size)
    if len(raw) != FOOTER.size:
        raise ValueError(f'incomplete or invalid RomFS trailer: {img}')
    t_magic, ver, gfx_api, flags, chain_len, base_sz, msl_off, msl_sz, chain_raw = FOOTER.unpack(raw)
    if (t_magic != MSL_MAGIC or ver != MSL_VERSION or gfx_api not in GFX_API_NAMES or
            base_sz != base_end or base_end > _U64_MAX - (MSL_ALIGN - 1) or
            msl_off != ((base_end + (MSL_ALIGN - 1)) & ~(MSL_ALIGN - 1)) or
            msl_sz < HEAD.size or msl_off > footer_off or msl_sz > footer_off - msl_off):
        raise ValueError(f'incomplete or invalid RomFS trailer: {img}')
    raw_end = msl_off + msl_sz
    if (raw_end > _U64_MAX - (MSL_FOOTER_ALIGN - 1) or
            ((raw_end + (MSL_FOOTER_ALIGN - 1)) & ~(MSL_FOOTER_ALIGN - 1)) != footer_off or
            chain_len > 24 or b'\0' in chain_raw[:chain_len] or
            chain_raw[chain_len:] != b'\0' * (24 - chain_len)):
        raise ValueError(f'incomplete or invalid RomFS trailer: {img}')
    try:
        chain_str = chain_raw[:chain_len].decode('ascii')
    except UnicodeDecodeError as exc:
        raise ValueError(f'non-ASCII chain in RomFS trailer: {img}') from exc
    return {
        'has_trailer': True,
        'version': ver,
        'gfx_api': GFX_API_NAMES[gfx_api],
        'flags': flags,
        'multi_program': bool(flags & MSL_FLAG_MULTI_PROGRAM),
        'chain': chain_str,
        'base_romfs_size': base_sz,
        'msl_offset': msl_off,
        'msl_size': msl_sz,
        'file_size': file_size,
    }


def _validate_and_list_slice(f, slice_offset, slice_size, label):
    """Validates a DMROMFS1 slice at [slice_offset, slice_offset + slice_size) and returns {rel_path: (is_dir, rel_off, size)}."""
    if slice_offset < 0 or slice_size < HEAD.size or slice_offset > _I64_MAX or slice_size > _I64_MAX - slice_offset:
        raise ValueError(f'invalid RomFS slice bounds: {label}')
    f.seek(slice_offset)
    head = f.read(HEAD.size)
    if len(head) != HEAD.size:
        raise ValueError(f'not a RomFS image: {label}')
    magic, ioff, isz, n = HEAD.unpack(head)
    if magic != MAGIC or not 0 < n <= 0xffffffff or ioff < HEAD.size or ioff > slice_size or isz != slice_size - ioff or n > isz // ENT.size:
        raise ValueError(f'incomplete or invalid RomFS image: {label}')
    f.seek(slice_offset + ioff)
    raw = f.read(isz)
    if len(raw) != isz:
        raise ValueError(f'incomplete RomFS index: {label}')
    pool_off = n * ENT.size
    pool_len = isz - pool_off
    e = [ENT.unpack_from(raw, k * ENT.size) for k in range(n)]
    if e[0][4] != 0 or e[0][3] != 0:
        raise ValueError(f'invalid RomFS root entry: {label}')
    next_child = 1
    names = []
    for k, (a, b, no, nl, t, flags) in enumerate(e):
        if no + nl > pool_len or t > 1 or flags > (1 if t else 0):
            raise ValueError(f'invalid RomFS entry {k}: {label}')
        nm = raw[pool_off + no:pool_off + no + nl]
        if k and (not nl or nl > 0x300 or b'\0' in nm or b'/' in nm or nm in (b'.', b'..')):
            raise ValueError(f'invalid RomFS entry name: {label}')
        names.append(nm)
        if t:
            if a < HEAD.size or a > ioff or b > ioff - a:
                raise ValueError(f'invalid RomFS file range: {label}')
        else:
            if a != next_child or a <= k or a > n or b > n - a:
                raise ValueError(f'invalid RomFS directory tree: {label}')
            next_child += b
    if next_child != n:
        raise ValueError(f'unreachable RomFS entries: {label}')
    for k, (a, b, _, _, t, _) in enumerate(e):
        if not t and any(names[j - 1] >= names[j] for j in range(a + 1, a + b)):
            raise ValueError(f'unsorted or duplicate RomFS names: {label}')
    out, todo = {}, [(0, '')]
    while todo:
        k, path = todo.pop()
        a, b, _, _, t, _ = e[k]
        if t == 0:
            if path:
                out[path] = (True, 0, 0)
            todo.extend((c, (path + '/' if path else '') + names[c].decode('utf-8', errors='surrogateescape')) for c in range(a, a + b))
        else:
            out[path] = (False, a, b)
    return out


def identity(img, title='', version='', build=''):
    """The identity line of a complete image (dm/host/romfs_identity.h defines the format and the C reader): its base RomFS size (ioff + isz)
    and an FNV-1a 64 hash of its header and index (the file data is not read, so a 14 GB image takes a moment). Raises ValueError for a cut off or invalid image."""
    size = os.path.getsize(img)
    with open(img, 'rb') as f:
        head = f.read(HEAD.size)
        if len(head) != HEAD.size: raise ValueError(f'not a RomFS image: {img}')
        magic, ioff, isz, _ = HEAD.unpack(head)
        if magic != MAGIC: raise ValueError(f'incomplete or invalid RomFS image: {img}')
        check_bounds_and_trailer(f, size, ioff, isz, img)
        base_size = ioff + isz
        h, rest = _FNV_OFFSET, b''
        f.seek(ioff)
        def mix(h, data):
            for w in struct.unpack(f'<{len(data) // 8}Q', data): h = ((h ^ w) * _FNV_PRIME) & _MASK
            return h
        h = mix(h, head)
        left = isz
        while left:
            chunk = f.read(min(left, 8 << 20))
            if not chunk: raise ValueError(f'incomplete RomFS image: {img}')
            left -= len(chunk)
            chunk = rest + chunk
            keep = len(chunk) % 8 if left else 0
            rest = chunk[len(chunk) - keep:]; chunk = chunk[:len(chunk) - keep]
            if not left and len(chunk) % 8: chunk += b'\0' * (8 - len(chunk) % 8)
            h = mix(h, chunk)
    return f'{ID_MAGIC} title={_clean(title)} version={_clean(version)} build={_clean(build)} size={base_size} index={h:016x}'


def _keep_msl_file(name, skip_suffix=()):
    if name.endswith('.img'):
        raise ValueError(f'nested .img is forbidden inside MSL trailer: {name}')
    if name.startswith('.') or '.tmp' in name or name in MSL_APP_LEAVE_OUT:
        return False
    if not name.endswith(MSL_APP_SUFFIXES):
        return False
    if skip_suffix and name.endswith(tuple(skip_suffix)):
        return False
    return True


def _collect_nvn_pair(folder_path, image_path, prefix, skip_suffix=()):
    """Reads a b-<chain> folder and/or b-<chain>.img using shader_lib.py's precedence, expanding into {prefix/name: source_spec}."""
    out = {}
    has_folder = folder_path is not None and os.path.isdir(folder_path)
    has_image = image_path is not None and os.path.isfile(image_path)
    folder_files = {}
    if has_folder:
        with os.scandir(folder_path) as it:
            for e in it:
                if e.name.startswith('.') or '.tmp' in e.name:
                    continue
                if e.is_dir(follow_symlinks=False):
                    raise ValueError(f'{e.path} is a folder: an NVN shader library holds only files')
                if e.name.endswith('.img'):
                    raise ValueError(f'nested .img is forbidden inside MSL trailer: {e.path}')
                folder_files[e.name] = e.path
    if has_image and (not has_folder or 'inputs.tsv' not in folder_files):
        img_size = os.path.getsize(image_path)
        with open(image_path, 'rb') as sf:
            listing = _validate_and_list_slice(sf, 0, img_size, image_path)
        for rel, (is_dir, off, sz) in listing.items():
            if is_dir:
                raise ValueError(f'{image_path} contains subdirectories; expected flat shader library image')
            if _keep_msl_file(rel, skip_suffix):
                out[f'{prefix}/{rel}'] = ('slice', image_path, off, sz)
    for fname, fpath in folder_files.items():
        if _keep_msl_file(fname, skip_suffix):
            out[f'{prefix}/{fname}'] = ('file', fpath)
    return out


def _collect_flat_dir(dir_path, prefix, skip_suffix=()):
    out = {}
    with os.scandir(dir_path) as it:
        for e in it:
            if e.name.startswith('.') or '.tmp' in e.name:
                continue
            if e.is_dir(follow_symlinks=False):
                raise ValueError(f'unexpected subdirectory inside {dir_path}: {e.path}')
            if _keep_msl_file(e.name, skip_suffix):
                out[f'{prefix}/{e.name}'] = ('file', e.path)
    return out


def _collect_msl_sources(msl_src, gfx_api=None, chain=None, multi_program=False, skip_suffix=()):
    src = os.fspath(msl_src)
    if not os.path.exists(src):
        raise FileNotFoundError(f'MSL source does not exist: {src}')
    files = {}
    seen_apis = set()
    seen_chains = set()
    inferred_multi = False

    if os.path.isfile(src):
        img_size = os.path.getsize(src)
        with open(src, 'rb') as sf:
            listing = _validate_and_list_slice(sf, 0, img_size, src)
        file_items = [(rel, off, sz) for rel, (is_dir, off, sz) in listing.items() if not is_dir]
        has_subdirs = any('/' in rel for rel, _, _ in file_items)
        if not has_subdirs:
            base = os.path.basename(src)
            stem = base[:-4] if base.endswith('.img') else base
            if chain:
                prefix = chain
                seen_apis.add('nvn'); seen_chains.add(chain)
            elif stem.startswith('b-'):
                prefix = stem
                seen_apis.add('nvn'); seen_chains.add(stem)
            elif gfx_api in ('vulkan', 2):
                prefix = 'vk'; seen_apis.add('vulkan')
            elif gfx_api in ('gl', 3):
                prefix = 'gl'; seen_apis.add('gl')
            else:
                raise ValueError(f'cannot infer shader directory prefix for flat image {src}; pass --chain b-<version> or --gfx-api')
            for rel, off, sz in file_items:
                if _keep_msl_file(rel, skip_suffix):
                    files[f'{prefix}/{rel}'] = ('slice', src, off, sz)
        else:
            for rel, off, sz in file_items:
                parts = rel.split('/')
                if any(p.endswith('.img') for p in parts):
                    raise ValueError(f'nested .img is forbidden inside MSL trailer: {rel}')
                if not _keep_msl_file(parts[-1], skip_suffix):
                    continue
                idx = 0
                if _PROG_DIR_RE.match(parts[0]) and len(parts) >= 3:
                    inferred_multi = True
                    idx = 1
                top = parts[idx]
                if top.startswith('b-'):
                    if chain and top != chain:
                        continue
                    seen_apis.add('nvn'); seen_chains.add(top)
                elif top == 'vk':
                    seen_apis.add('vulkan')
                elif top == 'gl':
                    seen_apis.add('gl')
                files[rel] = ('slice', src, off, sz)
    else:
        norm = os.path.normpath(src)
        base = os.path.basename(norm)
        if base.startswith('b-'):
            if chain and chain != base:
                raise ValueError(f'MSL directory {base} does not match --chain {chain}')
            sibling_img = norm + '.img'
            files.update(_collect_nvn_pair(norm, sibling_img if os.path.isfile(sibling_img) else None, base, skip_suffix))
            seen_apis.add('nvn'); seen_chains.add(base)
        elif base in ('vk', 'gl'):
            files.update(_collect_flat_dir(norm, base, skip_suffix))
            seen_apis.add('vulkan' if base == 'vk' else 'gl')
        else:
            def scan_program_root(root_dir, prog_prefix):
                nonlocal inferred_multi
                nvn_names = set()
                direct_files = []
                with os.scandir(root_dir) as it:
                    entries_list = sorted(it, key=lambda x: x.name)
                prog_dirs = [e for e in entries_list if not prog_prefix and e.is_dir(follow_symlinks=False) and _PROG_DIR_RE.match(e.name)]
                if prog_dirs:
                    inferred_multi = True
                    for pe in prog_dirs:
                        scan_program_root(pe.path, f'{pe.name}/')
                    for e in entries_list:
                        if e.name.startswith('.') or '.tmp' in e.name or (e in prog_dirs):
                            continue
                        if e.name.endswith('.img'):
                            raise ValueError(f'nested .img is forbidden inside multi-program root: {e.path}')
                    return
                for e in entries_list:
                    if e.name.startswith('.') or '.tmp' in e.name:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name in ('vk', 'gl'):
                            api_here = 'vulkan' if e.name == 'vk' else 'gl'
                            if gfx_api and GFX_API_NAMES.get(GFX_API_CODES.get(gfx_api, gfx_api)) != api_here:
                                continue
                            files.update(_collect_flat_dir(e.path, f'{prog_prefix}{e.name}', skip_suffix))
                            seen_apis.add(api_here)
                        elif e.name.startswith('b-'):
                            if not chain or e.name == chain:
                                nvn_names.add(e.name)
                        else:
                            raise ValueError(f'unexpected directory inside MSL source: {e.path}')
                    else:
                        if e.name.endswith('.img'):
                            stem = e.name[:-4]
                            if stem.startswith('b-'):
                                if not chain or stem == chain:
                                    nvn_names.add(stem)
                            else:
                                raise ValueError(f'nested .img is forbidden inside MSL source: {e.path}')
                        else:
                            direct_files.append(e)
                for lib_name in sorted(nvn_names):
                    if gfx_api and GFX_API_NAMES.get(GFX_API_CODES.get(gfx_api, gfx_api)) != 'nvn':
                        continue
                    fdir = os.path.join(root_dir, lib_name)
                    fimg = fdir + '.img'
                    files.update(_collect_nvn_pair(fdir if os.path.isdir(fdir) else None,
                                                   fimg if os.path.isfile(fimg) else None,
                                                   f'{prog_prefix}{lib_name}', skip_suffix))
                    seen_apis.add('nvn'); seen_chains.add(lib_name)
                if direct_files and not files:
                    eff_api = GFX_API_NAMES.get(GFX_API_CODES.get(gfx_api, gfx_api)) if gfx_api else None
                    if eff_api == 'nvn' and chain:
                        sub_prefix = f'{prog_prefix}{chain}'; seen_apis.add('nvn'); seen_chains.add(chain)
                    elif eff_api == 'vulkan':
                        sub_prefix = f'{prog_prefix}vk'; seen_apis.add('vulkan')
                    elif eff_api == 'gl':
                        sub_prefix = f'{prog_prefix}gl'; seen_apis.add('gl')
                    else:
                        raise ValueError(f'cannot infer target subdirectory for loose shader files in {root_dir}')
                    for e in direct_files:
                        if _keep_msl_file(e.name, skip_suffix):
                            files[f'{sub_prefix}/{e.name}'] = ('file', e.path)

            scan_program_root(norm, '')

    if not files:
        raise ValueError(f'no runtime shader files found in {src}')
    if isinstance(gfx_api, int):
        if gfx_api not in GFX_API_NAMES:
            raise ValueError(f'invalid gfx_api code: {gfx_api}')
        api_name = GFX_API_NAMES[gfx_api]
    elif gfx_api is not None:
        if gfx_api not in GFX_API_CODES:
            raise ValueError(f'invalid gfx_api {gfx_api!r}: expected one of {tuple(GFX_API_CODES)}')
        api_name = gfx_api
    else:
        if len(seen_apis) != 1:
            raise ValueError(f'cannot uniquely infer gfx_api from {src} (found {sorted(seen_apis)}); pass --gfx-api')
        api_name = next(iter(seen_apis))

    if chain is None:
        if api_name == 'nvn':
            if len(seen_chains) != 1:
                raise ValueError(f'cannot uniquely infer NVN chain from {src} (found {sorted(seen_chains)}); pass --chain')
            chain_str = next(iter(seen_chains))
        else:
            chain_str = next(iter(seen_chains)) if len(seen_chains) == 1 else ''
    else:
        chain_str = str(chain)

    if api_name == 'nvn' and not chain_str:
        raise ValueError('NVN shader trailer requires a non-empty chain identifier')
    try:
        chain_bytes = chain_str.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError(f'chain must be ASCII: {chain_str!r}') from exc
    if len(chain_bytes) > 24 or b'\0' in chain_bytes or b'\n' in chain_bytes or b'\r' in chain_bytes:
        raise ValueError(f'invalid chain {chain_str!r} (must be <= 24 ASCII bytes without NUL or newline)')

    return files, GFX_API_CODES[api_name], chain_bytes, bool(multi_program or inferred_multi)


def _write_msl_subimage(out_file, base_offset, files_map):
    """Writes a tree-structured DMROMFS1 sub-image starting at base_offset in out_file with 16-byte aligned file payloads.
    Returns the sub-image byte length."""
    tree = {}   # dir_tuple -> set of (child_name_bytes, is_dir, full_rel_str)
    tree[()] = set()
    for rel in files_map:
        parts = [p for p in rel.split('/') if p]
        if not parts:
            raise ValueError(f'empty relative path in MSL trailer: {rel!r}')
        for part in parts:
            bpart = part.encode('utf-8')
            if not bpart or len(bpart) > 0x300 or b'\0' in bpart or b'/' in bpart or bpart in (b'.', b'..'):
                raise ValueError(f'invalid path component in MSL trailer: {part!r}')
        for depth in range(len(parts) - 1):
            parent = tuple(parts[:depth])
            cur = tuple(parts[:depth + 1])
            tree.setdefault(parent, set()).add((parts[depth].encode('utf-8'), True, cur))
            tree.setdefault(cur, set())
        parent = tuple(parts[:-1])
        tree.setdefault(parent, set()).add((parts[-1].encode('utf-8'), False, rel))

    ents = [[b'', (), True, 0, 0]]   # [name_bytes, key, is_dir, a, b]
    i = 0
    while i < len(ents):
        _, key, is_dir, _, _ = ents[i]
        if is_dir:
            kids = sorted(tree.get(key, ()), key=lambda item: item[0])
            for j in range(1, len(kids)):
                if kids[j - 1][0] == kids[j][0]:
                    raise ValueError(f'duplicate entry name in MSL trailer: {kids[j][0]!r}')
            ents[i][3], ents[i][4] = len(ents), len(kids)
            for cname, cis_dir, ckey in kids:
                ents.append([cname, ckey, cis_dir, 0, 0])
        i += 1

    out_file.seek(base_offset)
    out_file.write(b'\0' * HEAD.size)
    off = HEAD.size
    for e in ents:
        if e[2]:
            continue
        pad = (-off) % MSL_DATA_ALIGN
        if pad:
            out_file.write(b'\0' * pad)
            off += pad
        spec = files_map[e[1]]
        written = 0
        if spec[0] == 'file':
            with open(spec[1], 'rb') as rf:
                while chunk := rf.read(8 << 20):
                    out_file.write(chunk)
                    written += len(chunk)
        elif spec[0] == 'slice':
            _, spath, soff, ssz = spec
            with open(spath, 'rb') as rf:
                rf.seek(soff)
                left = ssz
                while left:
                    chunk = rf.read(min(left, 8 << 20))
                    if not chunk:
                        raise ValueError(f'unexpected EOF reading {spath}')
                    out_file.write(chunk)
                    written += len(chunk)
                    left -= len(chunk)
        else:
            data = spec[1]
            out_file.write(data)
            written = len(data)
        e[3], e[4] = off, written
        off += written

    head, idx = layout(ents, off)
    out_file.write(idx)
    msl_size = off + len(idx)
    out_file.seek(base_offset)
    out_file.write(head)
    out_file.seek(base_offset + msl_size)
    return msl_size


def _write_trailer(img, msl_src, gfx_api=None, chain=None, multi_program=False, skip_suffix=(), require_valid_existing=True):
    img_path = os.fspath(img)
    files_map, api_code, chain_bytes, is_multi = _collect_msl_sources(
        msl_src, gfx_api=gfx_api, chain=chain, multi_program=multi_program, skip_suffix=skip_suffix)

    file_size = os.path.getsize(img_path)
    with open(img_path, 'r+b') as f:
        head = f.read(HEAD.size)
        if len(head) != HEAD.size:
            raise ValueError(f'not a RomFS image: {img_path}')
        magic, ioff, isz, _ = HEAD.unpack(head)
        if magic != MAGIC or file_size > _I64_MAX or ioff < HEAD.size or ioff > file_size or isz > file_size - ioff:
            raise ValueError(f'incomplete or invalid RomFS image: {img_path}')
        if require_valid_existing:
            check_bounds_and_trailer(f, file_size, ioff, isz, img_path)
        base_romfs_size = ioff + isz
        _validate_and_list_slice(f, 0, base_romfs_size, img_path)

        if base_romfs_size > _U64_MAX - (MSL_ALIGN - 1):
            raise ValueError(f'base RomFS size overflows page alignment: {img_path}')
        msl_offset = (base_romfs_size + (MSL_ALIGN - 1)) & ~(MSL_ALIGN - 1)

        f.truncate(base_romfs_size)
        f.seek(base_romfs_size)
        if msl_offset > base_romfs_size:
            f.write(b'\0' * (msl_offset - base_romfs_size))

        msl_size = _write_msl_subimage(f, msl_offset, files_map)
        raw_end = msl_offset + msl_size
        footer_off = (raw_end + (MSL_FOOTER_ALIGN - 1)) & ~(MSL_FOOTER_ALIGN - 1)
        if footer_off > raw_end:
            f.write(b'\0' * (footer_off - raw_end))

        f.flush()
        os.fsync(f.fileno())

        flags = MSL_FLAG_MULTI_PROGRAM if is_multi else 0
        footer = FOOTER.pack(
            MSL_MAGIC,
            MSL_VERSION,
            api_code,
            flags,
            len(chain_bytes),
            base_romfs_size,
            msl_offset,
            msl_size,
            chain_bytes.ljust(24, b'\0'),
        )
        f.write(footer)
        f.flush()
        os.fsync(f.fileno())

    return inspect_msl(img_path)


def append_msl(img, msl_src, gfx_api=None, chain=None, multi_program=False, skip_suffix=()):
    """Validates img (rejecting truncated base images or broken trailers), truncates to base_romfs_size, writes a 16 KiB-aligned
    DMROMFS1 shader sub-image and publishes the 64-byte DMMSLTR1 footer after fsync."""
    return _write_trailer(img, msl_src, gfx_api=gfx_api, chain=chain, multi_program=multi_program,
                          skip_suffix=skip_suffix, require_valid_existing=True)


def replace_msl(img, msl_src, gfx_api=None, chain=None, multi_program=False, skip_suffix=()):
    """Validates the base DMROMFS1 image of img, truncates at base_romfs_size (discarding any previous or interrupted trailer),
    writes the new 16 KiB-aligned shader sub-image and publishes the 64-byte DMMSLTR1 footer after fsync."""
    return _write_trailer(img, msl_src, gfx_api=gfx_api, chain=chain, multi_program=multi_program,
                          skip_suffix=skip_suffix, require_valid_existing=False)


def inspect_msl(img):
    """Inspects img and returns a metadata dict describing its base_romfs_size and optional DMMSLTR1 trailer.
    Raises ValueError if the base image, trailer footer or embedded sub-image is truncated or invalid."""
    img_path = os.fspath(img)
    file_size = os.path.getsize(img_path)
    with open(img_path, 'rb') as f:
        head = f.read(HEAD.size)
        if len(head) != HEAD.size:
            raise ValueError(f'not a RomFS image: {img_path}')
        magic, ioff, isz, _ = HEAD.unpack(head)
        if magic != MAGIC:
            raise ValueError(f'not a RomFS image: {img_path}')
        info = check_bounds_and_trailer(f, file_size, ioff, isz, img_path)
        base_end = ioff + isz
        if info is None:
            return {
                'has_trailer': False,
                'version': 0,
                'gfx_api': None,
                'flags': 0,
                'multi_program': False,
                'chain': '',
                'base_romfs_size': base_end,
                'msl_offset': 0,
                'msl_size': 0,
                'file_size': file_size,
                'files': 0,
            }
        listing = _validate_and_list_slice(f, info['msl_offset'], info['msl_size'], f'{img_path} [msl trailer]')
        info['files'] = sum(1 for is_dir, _, _ in listing.values() if not is_dir)
        return info


def _validate_extract_rel_path(rel_path, label):
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError(f'empty relative path in RomFS trailer: {label}')
    if rel_path.startswith(('/', '\\')) or '\\' in rel_path or ':' in rel_path or '\0' in rel_path:
        raise ValueError(f'unsafe relative path {rel_path!r} in RomFS trailer: {label}')
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in rel_path):
        raise ValueError(f'non-UTF-8 path in RomFS trailer: {label}')
    parts = rel_path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'unsafe path segment in {rel_path!r} in RomFS trailer: {label}')
    return parts


def _resolve_extract_dest(out_real, parts, label):
    cur = out_real
    for seg in parts:
        cur = os.path.join(cur, seg)
        if os.path.islink(cur):
            raise ValueError(f'symlink component forbidden in extraction path {cur}: {label}')
    cand = os.path.abspath(cur)
    if cand == out_real or os.path.commonpath([out_real, cand]) != out_real:
        raise ValueError(f'extraction path escapes output directory: {label}')
    return cand


def extract_msl(img, out_dir):
    """Validates and streams the embedded DMMSLTR1 shader trailer of img into out_dir."""
    img_path = os.fspath(img)
    out_path = os.fspath(out_dir)
    if not out_path or '\0' in out_path:
        raise ValueError('invalid extraction output directory')
    info = inspect_msl(img_path)
    if not info['has_trailer']:
        raise ValueError(f'no DMMSLTR1 shader trailer in {img}')
    if os.path.islink(out_path):
        raise ValueError(f'extraction output directory must not be a symlink: {out_path}')
    if os.path.exists(out_path) and not os.path.isdir(out_path):
        raise ValueError(f'extraction output path is not a directory: {out_path}')

    msl_offset = info['msl_offset']
    msl_size = info['msl_size']
    label = f'{img_path} [msl trailer]'

    with open(img_path, 'rb') as f:
        if os.fstat(f.fileno()).st_size != info['file_size']:
            raise ValueError(f'RomFS image size changed during extraction: {img_path}')
        listing = _validate_and_list_slice(f, msl_offset, msl_size, label)
        if len(listing) > MSL_EXTRACT_MAX_ENTRIES:
            raise ValueError(f'RomFS trailer exceeds entry quota ({len(listing)} > {MSL_EXTRACT_MAX_ENTRIES}): {img_path}')

        dirs_todo = []
        files_todo = []
        count = 0
        total_bytes = 0
        for rel_path, (is_dir, rel_off, size) in sorted(listing.items()):
            parts = _validate_extract_rel_path(rel_path, label)
            if is_dir:
                dirs_todo.append(parts)
            else:
                if rel_off < HEAD.size or size < 0 or rel_off > msl_size or size > msl_size - rel_off:
                    raise ValueError(f'invalid RomFS trailer file bounds for {rel_path!r}: {img_path}')
                if size > MSL_EXTRACT_MAX_BYTES - total_bytes:
                    raise ValueError(f'RomFS trailer exceeds byte quota ({MSL_EXTRACT_MAX_BYTES} bytes): {img_path}')
                total_bytes += size
                count += 1
                files_todo.append((msl_offset + rel_off, size, parts, rel_path))

        os.makedirs(out_path, exist_ok=True)
        out_real = os.path.realpath(out_path)

        resolved_dirs = []
        for parts in dirs_todo:
            d_dest = _resolve_extract_dest(out_real, parts, label)
            if os.path.exists(d_dest) and not os.path.isdir(d_dest):
                raise ValueError(f'cannot overwrite file with directory {d_dest}: {img_path}')
            resolved_dirs.append(d_dest)

        resolved_files = []
        for abs_off, size, parts, rel_path in files_todo:
            f_dest = _resolve_extract_dest(out_real, parts, label)
            if os.path.isdir(f_dest):
                raise ValueError(f'cannot overwrite directory with file {f_dest}: {img_path}')
            resolved_files.append((abs_off, size, f_dest, rel_path))

        for d_dest in resolved_dirs:
            os.makedirs(d_dest, exist_ok=True)
            if os.path.commonpath([out_real, os.path.realpath(d_dest)]) != out_real:
                raise ValueError(f'directory resolved outside output root: {d_dest}')

        created_parents = set()
        resolved_files.sort(key=lambda item: (item[0], item[3]))
        pid = os.getpid()
        for abs_off, size, f_dest, rel_path in resolved_files:
            parent = os.path.dirname(f_dest)
            if parent not in created_parents:
                os.makedirs(parent, exist_ok=True)
                if os.path.commonpath([out_real, os.path.realpath(parent)]) != out_real:
                    raise ValueError(f'parent directory resolved outside output root: {parent}')
                created_parents.add(parent)
            if os.path.islink(f_dest):
                raise ValueError(f'refusing to overwrite symlink {f_dest}: {img_path}')
            tmp_dest = f'{f_dest}.part.{pid}'
            if os.path.islink(tmp_dest):
                raise ValueError(f'refusing to write through symlink {tmp_dest}: {img_path}')
            f.seek(abs_off)
            rem = size
            try:
                with open(tmp_dest, 'wb') as out_f:
                    while rem > 0:
                        chunk = f.read(min(rem, MSL_EXTRACT_CHUNK))
                        if not chunk:
                            raise ValueError(f'unexpected EOF reading {rel_path!r} from RomFS trailer: {img_path}')
                        out_f.write(chunk)
                        rem -= len(chunk)
                os.replace(tmp_dest, f_dest)
            except BaseException:
                try:
                    os.unlink(tmp_dest)
                except OSError:
                    pass
                raise

    return {
        **info,
        'out_dir': os.path.abspath(out_path),
        'extracted_files': count,
        'extracted_bytes': total_bytes,
    }


def parse_identity(text):   # {field: value} of an identity line; size is an int
    parts = text.split()
    if not parts or parts[0] != ID_MAGIC: raise ValueError('not an identity line')
    d = dict(p.split('=', 1) for p in parts[1:] if '=' in p)
    if 'size' not in d or 'index' not in d: raise ValueError('identity line without size or index')
    d['size'] = int(d['size']); return d


def same_identity(a, b):   # two lines name the same image when size and index agree (title, version and build only describe it)
    x, y = parse_identity(a), parse_identity(b)
    return (x['size'], x['index']) == (y['size'], y['index'])


def _order_trailer_args(first, second):
    if os.path.isdir(first) and os.path.isfile(second):
        return second, first
    return first, second


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['--identity']:   # romfs_image.py --identity IMAGE [TITLE [VERSION [BUILD]]]: the identity line of IMAGE
        try: print(identity(*sys.argv[2:6]))
        except (OSError, ValueError, TypeError) as e: sys.exit(f'[RED] {e}')
        sys.exit(0)
    ap = argparse.ArgumentParser(description='Pack a RomFS folder into an indexed image or manage its DMMSLTR1 shader trailer')
    ap.add_argument('--reuse', action='store_true', help='keep the image only while its source tree, packer and image are unchanged')
    ap.add_argument('--append-msl', action='store_true', help='append a 16 KiB-aligned DMMSLTR1 shader trailer to IMAGE from MSL_SRC')
    ap.add_argument('--replace-msl', action='store_true', help='truncate IMAGE to base_romfs_size and write a new DMMSLTR1 shader trailer from MSL_SRC')
    ap.add_argument('--extract-msl', action='store_true', help='extract the DMMSLTR1 shader trailer from IMAGE into OUT_DIR')
    ap.add_argument('--inspect-msl', metavar='IMAGE', help='inspect IMAGE and print its DMMSLTR1 trailer metadata as JSON')
    ap.add_argument('--gfx-api', choices=tuple(GFX_API_CODES), help='graphics backend for --append-msl / --replace-msl (inferred when unambiguous)')
    ap.add_argument('--chain', help='shader chain identifier (e.g. b-<hex>) for --append-msl / --replace-msl')
    ap.add_argument('--multi-program', action='store_true', help='mark the trailer as multi-program (p<N>/ prefixes)')
    ap.add_argument('--skip-suffix', action='append', default=[], help='file suffix to omit when packing MSL files (may be repeated)')
    ap.add_argument('pos', nargs='*')
    args = ap.parse_args()
    try:
        if args.inspect_msl:
            if args.pos or args.append_msl or args.replace_msl or args.extract_msl:
                raise ValueError('--inspect-msl takes a single IMAGE argument')
            print(json.dumps(inspect_msl(args.inspect_msl), indent=2))
            sys.exit(0)
        trailer_modes = int(bool(args.append_msl)) + int(bool(args.replace_msl)) + int(bool(args.extract_msl))
        if trailer_modes > 1:
            raise ValueError('--append-msl, --replace-msl and --extract-msl are mutually exclusive')
        if args.extract_msl:
            if len(args.pos) != 2:
                raise ValueError('expected IMAGE and OUT_DIR arguments')
            res = extract_msl(args.pos[0], args.pos[1])
            print(f"{args.pos[0]} -> {res['out_dir']}: extracted {res['extracted_files']} files, {res['extracted_bytes']} bytes ({res['gfx_api']} {res['chain'] or '-'})")
            sys.exit(0)
        if args.append_msl or args.replace_msl:
            if len(args.pos) != 2:
                raise ValueError('expected IMAGE and MSL_SRC arguments')
            target_img, msl_src = _order_trailer_args(args.pos[0], args.pos[1])
            fn = append_msl if args.append_msl else replace_msl
            info = fn(target_img, msl_src, gfx_api=args.gfx_api, chain=args.chain,
                      multi_program=args.multi_program, skip_suffix=args.skip_suffix)
            print(f"{target_img}: trailer {info['gfx_api']} {info['chain'] or '-'} ({info['files']} files, {info['msl_size']} bytes at {info['msl_offset']})")
            sys.exit(0)
        if len(args.pos) != 2:
            ap.error('the following arguments are required: source, image')
        n = pack(args.pos[0], args.pos[1], reuse=args.reuse)
        print(f'{args.pos[1]}: {n} entries, {os.path.getsize(args.pos[1])} bytes')
    except (OSError, ValueError, TypeError) as e:
        sys.exit(f'[RED] {e}')
