// DMROMFS1's index loader, shared by the host mount and iOS startup. Startup must validate the same index before replacing a working folder.
#pragma once
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

typedef struct { uint64_t a, b; uint32_t name; uint16_t nlen; uint8_t type, flags; } __attribute__((packed)) img_ent;   // directory: a = first child, b = children; file: a = offset, b = size
_Static_assert(sizeof(img_ent) == 24, "RomFS image entry layout");
#define IMG_ENT_DAMAGED 1    // in the image: this file failed the NCA's hash check and was written as read (nx_extract --allow-damaged)
#define IMG_ENT_REPORTED 2   // in memory only: the host has named this damaged file in its log
typedef struct { int fd; uint64_t n, damaged; img_ent *e; const char *names; } romfs_img;
typedef romfs_img romfs_image;

#define DM_MSL_MAGIC "DMMSLTR1"
#define DM_MSL_VERSION 1u
#define DM_MSL_ALIGN 16384ull
#define DM_MSL_FOOTER_ALIGN 8ull
#define DM_MSL_FLAG_MULTI_PROGRAM 1u
enum { DM_MSL_GFX_NVN = 1, DM_MSL_GFX_VULKAN = 2, DM_MSL_GFX_GL = 3 };

typedef struct __attribute__((packed)) {
    char     magic[8];          // "DMMSLTR1", written last so an interrupted append is rejected as incomplete
    uint32_t version;           // 1
    uint8_t  gfx_api;           // 1 = nvn, 2 = vulkan, 3 = gl
    uint8_t  flags;             // bit 0: multi_program (p<N>/ prefixes)
    uint16_t chain_len;         // valid byte count in chain (0..24)
    uint64_t base_romfs_size;   // must equal ioff + isz of the base DMROMFS1 header
    uint64_t msl_offset;        // align_up(base_romfs_size, 16384)
    uint64_t msl_size;          // byte size of the embedded DMROMFS1 shader sub-image (>= 32)
    char     chain[24];         // NUL-padded chain string, e.g. "b-d0e99bd4b2fe"
} dm_msl_footer;
_Static_assert(sizeof(dm_msl_footer) == 64, "DMMSLTR1 footer must be 64 bytes");

static inline void romfs_image_close(romfs_img *m) {
    if (m) { close(m->fd); free(m->e); free(m); }
}
static inline int romfs_image_read(int fd, void *buf, size_t size, off_t off) {
    size_t got = 0;
    while (got < size) {
        ssize_t n = pread(fd, (char *)buf + got, size - got, off + (off_t)got);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { if (!n) errno = EINVAL; return 0; }
        got += (size_t)n;
    }
    return 1;
}
// Validates [0, ioff + isz) and any optional DMMSLTR1 shader trailer in [ioff + isz, file_size).
// When file_size == ioff + isz, zeroes *out_footer (if non-NULL) and returns 1.
// When file_size > ioff + isz, validates the 64-byte DMMSLTR1 footer at EOF and stores it in *out_footer (if non-NULL).
// Returns 0 with errno = EINVAL on any overflow, truncation or malformed trailer.
static inline int romfs_check_bounds_and_trailer(int fd, uint64_t file_size, uint64_t ioff, uint64_t isz, dm_msl_footer *out_footer) {
    if (out_footer) memset(out_footer, 0, sizeof *out_footer);
    if (file_size > (uint64_t)INT64_MAX || ioff < 32ull || ioff > file_size || isz > file_size - ioff) {
        errno = EINVAL;
        return 0;
    }
    uint64_t base_end = ioff + isz;
    if (file_size == base_end) return 1;
    if (file_size - base_end < sizeof(dm_msl_footer)) {
        errno = EINVAL;
        return 0;
    }
    uint64_t footer_off = file_size - sizeof(dm_msl_footer);
    dm_msl_footer f;
    if (!romfs_image_read(fd, &f, sizeof f, (off_t)footer_off)) {
        if (!errno) errno = EINVAL;
        return 0;
    }
    if (memcmp(f.magic, DM_MSL_MAGIC, 8) != 0 ||
        f.version != DM_MSL_VERSION ||
        f.gfx_api < DM_MSL_GFX_NVN || f.gfx_api > DM_MSL_GFX_GL ||
        f.base_romfs_size != base_end ||
        base_end > UINT64_MAX - (DM_MSL_ALIGN - 1ull) ||
        f.msl_offset != ((base_end + (DM_MSL_ALIGN - 1ull)) & ~(DM_MSL_ALIGN - 1ull)) ||
        f.msl_size < 32ull ||
        f.msl_offset > footer_off ||
        f.msl_size > footer_off - f.msl_offset) {
        errno = EINVAL;
        return 0;
    }
    uint64_t raw_end = f.msl_offset + f.msl_size;
    if (raw_end > UINT64_MAX - (DM_MSL_FOOTER_ALIGN - 1ull) ||
        ((raw_end + (DM_MSL_FOOTER_ALIGN - 1ull)) & ~(DM_MSL_FOOTER_ALIGN - 1ull)) != footer_off ||
        f.chain_len > sizeof f.chain) {
        errno = EINVAL;
        return 0;
    }
    for (uint16_t i = 0; i < f.chain_len; i++) {
        if (f.chain[i] == '\0') { errno = EINVAL; return 0; }
    }
    for (uint16_t i = f.chain_len; i < sizeof f.chain; i++) {
        if (f.chain[i] != '\0') { errno = EINVAL; return 0; }
    }
    if (out_footer) *out_footer = f;
    return 1;
}
// Opens and validates a DMROMFS1 image located at [slice_off, slice_off + slice_size) inside fd.
// On success, takes ownership of fd (closed by romfs_image_close). On failure, leaves fd open for the caller to close.
static inline romfs_image *romfs_image_open_slice(int fd, uint64_t slice_off, uint64_t slice_size) {
    uint8_t h[32]; romfs_img *m = NULL; uint64_t ioff, isz, n;
    if (fd < 0) { errno = EBADF; return NULL; }
    if (slice_size < sizeof h || slice_off > (uint64_t)INT64_MAX || slice_size > (uint64_t)INT64_MAX - slice_off) {
        errno = EINVAL;
        return NULL;
    }
    if (!romfs_image_read(fd, h, sizeof h, (off_t)slice_off)) return NULL;
    errno = EINVAL;
    if (memcmp(h, "DMROMFS1", 8)) goto bad;
    memcpy(&ioff, h + 8, 8); memcpy(&isz, h + 16, 8); memcpy(&n, h + 24, 8);
    if (!n || n > UINT32_MAX || ioff < sizeof h || ioff > slice_size || isz != slice_size - ioff || isz > SIZE_MAX || n > isz / sizeof(img_ent)) goto bad;
    if (!(m = calloc(1, sizeof *m))) goto bad;
    if (!(m->e = malloc((size_t)isz)) || !romfs_image_read(fd, m->e, (size_t)isz, (off_t)(slice_off + ioff))) goto bad;
    m->n = n; m->names = (const char *)m->e + n * sizeof(img_ent);
    uint64_t pool = isz - n * sizeof(img_ent), next_child = 1;
    errno = EINVAL;
    if (m->e[0].type || m->e[0].nlen) goto bad;
    for (uint64_t k = 0; k < n; k++) {
        const img_ent *x = &m->e[k];
        if ((uint64_t)x->name + x->nlen > pool || x->type > 1 || x->flags > (x->type ? IMG_ENT_DAMAGED : 0)) goto bad;
        m->damaged += x->flags;
        const char *name = m->names + x->name;
        if (k && (!x->nlen || x->nlen > 0x300 || memchr(name, 0, x->nlen) || memchr(name, '/', x->nlen) || (x->nlen == 1 && name[0] == '.') || (x->nlen == 2 && !memcmp(name, "..", 2)))) goto bad;
        if (x->type) { if (x->a < sizeof h || x->a > ioff || x->b > ioff - x->a) goto bad; }
        else {
            // Breadth-first child runs partition entries 1..n-1; reject cycles, aliases and unreachable entries.
            if (x->a != next_child || x->a <= k || x->a > n || x->b > n - x->a) goto bad;
            next_child += x->b;
        }
    }
    if (next_child != n) goto bad;
    for (uint64_t k = 0; k < n; k++) {
        const img_ent *d = &m->e[k]; if (d->type) continue;
        for (uint64_t j = 1; j < d->b; j++) {
            const img_ent *a = &m->e[d->a + j - 1], *b = a + 1;
            size_t len = a->nlen < b->nlen ? a->nlen : b->nlen;
            int c = memcmp(m->names + a->name, m->names + b->name, len);
            if (c > 0 || (!c && a->nlen >= b->nlen)) goto bad;
        }
    }
    m->fd = fd;
    return m;
bad: {
    int e = errno ? errno : EINVAL;
    if (m) { free(m->e); free(m); }
    errno = e; return NULL;
    }
}
// NULL for an incomplete or invalid image (errno EINVAL), an I/O error, or allocation failure. The caller owns the descriptor and index on success.
static inline romfs_img *romfs_image_open(const char *path) {
    int fd = open(path, O_RDONLY); struct stat st; uint8_t h[32]; uint64_t ioff, isz;
    if (fd < 0) return NULL;
    if (fstat(fd, &st) || !romfs_image_read(fd, h, sizeof h, 0)) goto bad;
    errno = EINVAL;
    if (memcmp(h, "DMROMFS1", 8) || st.st_size < (off_t)sizeof h) goto bad;
    memcpy(&ioff, h + 8, 8); memcpy(&isz, h + 16, 8);
    if (!romfs_check_bounds_and_trailer(fd, (uint64_t)st.st_size, ioff, isz, NULL)) goto bad;
    romfs_img *m = romfs_image_open_slice(fd, 0, ioff + isz);
    if (!m) goto bad;
    return m;
bad: {
    int e = errno ? errno : EINVAL;
    close(fd);
    errno = e; return NULL;
    }
}
