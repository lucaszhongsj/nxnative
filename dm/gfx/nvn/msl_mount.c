// Mounted DMMSLTR1 shader trailer inside DM_ROMFS, shared across NVN, Vulkan and OpenGL on Metal.
// Lives in dm/gfx/nvn/ so existing host, test, iOS and Windows wildcards compile it without touching Makefiles or dmsh_chain.
#include "../msl_mount.h"
#include "../nvn/msl_image.h"
#include <pthread.h>
#include <stdio.h>

static pthread_mutex_t g_msl_tr_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_msl_tr_cv = PTHREAD_COND_INITIALIZER;
static int g_msl_tr_state;          // 0 = uninitialized, 1 = mounted, -1 = absent or invalid
static uint32_t g_msl_tr_inflight;  // active pread / mmap calls holding g_msl_tr_img->fd
static char *g_msl_tr_path;
static romfs_image *g_msl_tr_img;
static dm_msl_footer g_msl_tr_footer;

static void msl_tr_reset_locked(void) {
    while (g_msl_tr_inflight) pthread_cond_wait(&g_msl_tr_cv, &g_msl_tr_mu);
    if (g_msl_tr_img) {
        romfs_image_close(g_msl_tr_img);
        g_msl_tr_img = NULL;
    }
    free(g_msl_tr_path);
    g_msl_tr_path = NULL;
    memset(&g_msl_tr_footer, 0, sizeof g_msl_tr_footer);
    g_msl_tr_state = 0;
}

static int msl_tr_ensure_locked(void) {
    const char *env = getenv("DM_ROMFS");
    if (g_msl_tr_state != 0) {
        int same = (env && *env) ? (g_msl_tr_path && !strcmp(env, g_msl_tr_path)) : (g_msl_tr_path == NULL);
        if (same) return g_msl_tr_state == 1;
        msl_tr_reset_locked();
    }
    if (!env || !*env) {
        g_msl_tr_state = -1;
        return 0;
    }
    g_msl_tr_path = strdup(env);
    if (!g_msl_tr_path) {
        errno = ENOMEM;
        g_msl_tr_state = -1;
        return 0;
    }
    int fd = open(env, O_RDONLY);
    if (fd < 0) {
        g_msl_tr_state = -1;
        return 0;
    }
    struct stat st;
    uint8_t h[32];
    uint64_t ioff, isz;
    dm_msl_footer f;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < (off_t)sizeof h ||
        !romfs_image_read(fd, h, sizeof h, 0) || memcmp(h, "DMROMFS1", 8) != 0) {
        close(fd);
        errno = EINVAL;
        g_msl_tr_state = -1;
        return 0;
    }
    memcpy(&ioff, h + 8, 8);
    memcpy(&isz, h + 16, 8);
    if (!romfs_check_bounds_and_trailer(fd, (uint64_t)st.st_size, ioff, isz, &f) || !f.msl_size) {
        int e = errno ? errno : ENOENT;
        close(fd);
        errno = e;
        g_msl_tr_state = -1;
        return 0;
    }
    romfs_image *img = romfs_image_open_slice(fd, f.msl_offset, f.msl_size);
    if (!img) {
        int e = errno ? errno : EINVAL;
        close(fd);
        errno = e;
        g_msl_tr_state = -1;
        return 0;
    }
    g_msl_tr_img = img;
    g_msl_tr_footer = f;
    g_msl_tr_state = 1;
    return 1;
}

static const img_ent *msl_tr_find_exact(const romfs_image *m, const char *rel_path) {
    if (!m || !rel_path) return NULL;
    while (*rel_path == '/') rel_path++;
    if (!*rel_path) return NULL;
    uint64_t cur = 0;
    const char *p = rel_path;
    while (*p) {
        const char *slash = strchr(p, '/');
        size_t len = slash ? (size_t)(slash - p) : strlen(p);
        const char *next = slash ? slash + 1 : p + len;
        while (*next == '/') next++;
        if (!len || (len == 1 && p[0] == '.') || (len == 2 && p[0] == '.' && p[1] == '.')) return NULL;
        const img_ent *dir = &m->e[cur];
        if (dir->type != 0) return NULL;
        uint64_t lo = dir->a, hi = lo + dir->b, hit_idx = 0;
        const img_ent *hit = NULL;
        while (lo < hi) {
            uint64_t mid = lo + (hi - lo) / 2;
            const img_ent *x = &m->e[mid];
            size_t cmp_len = x->nlen < len ? x->nlen : len;
            int c = memcmp(m->names + x->name, p, cmp_len);
            if (!c) c = x->nlen < len ? -1 : (x->nlen > len ? 1 : 0);
            if (!c) { hit = x; hit_idx = mid; break; }
            if (c < 0) lo = mid + 1;
            else hi = mid;
        }
        if (!hit) return NULL;
        if (!*next) return hit->type == 1 ? hit : NULL;
        if (hit->type != 0) return NULL;
        cur = hit_idx;
        p = next;
    }
    return NULL;
}

static const img_ent *msl_tr_resolve_locked(const char *rel_path) {
    if (!rel_path) return NULL;
    while (*rel_path == '/') rel_path++;
    if (!*rel_path) return NULL;
    if (g_msl_tr_footer.flags & DM_MSL_FLAG_MULTI_PROGRAM) {
        const char *prog = getenv("DM_PROGRAM");
        const char *tag = (!prog || !*prog || !strcmp(prog, "0")) ? "0" : prog;
        if (!strchr(tag, '/')) {
            char buf[1024];
            int n = snprintf(buf, sizeof buf, "p%s/%s", tag, rel_path);
            if (n > 0 && (size_t)n < sizeof buf) {
                const img_ent *pref = msl_tr_find_exact(g_msl_tr_img, buf);
                if (pref) return pref;
            }
        }
    }
    return msl_tr_find_exact(g_msl_tr_img, rel_path);
}

__attribute__((visibility("hidden"))) int dm_msl_trailer_info(dm_msl_footer *out_footer) {
    pthread_mutex_lock(&g_msl_tr_mu);
    int ok = msl_tr_ensure_locked();
    if (ok && out_footer) *out_footer = g_msl_tr_footer;
    else if (out_footer) memset(out_footer, 0, sizeof *out_footer);
    pthread_mutex_unlock(&g_msl_tr_mu);
    return ok;
}

__attribute__((visibility("hidden"))) int dm_msl_trailer_has(const char *rel_path) {
    if (!rel_path) return 0;
    pthread_mutex_lock(&g_msl_tr_mu);
    int ok = msl_tr_ensure_locked() && msl_tr_resolve_locked(rel_path) != NULL;
    pthread_mutex_unlock(&g_msl_tr_mu);
    return ok;
}

__attribute__((visibility("hidden"))) int dm_msl_trailer_read(const char *rel_path, void **out_buf, size_t *out_len) {
    if (!rel_path || !out_buf || !out_len) { errno = EINVAL; return 0; }
    *out_buf = NULL;
    *out_len = 0;
    pthread_mutex_lock(&g_msl_tr_mu);
    if (!msl_tr_ensure_locked()) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        return 0;
    }
    const img_ent *x = msl_tr_resolve_locked(rel_path);
    if (!x) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        errno = ENOENT;
        return 0;
    }
    uint64_t abs_off = g_msl_tr_footer.msl_offset + x->a;
    uint64_t size = x->b;
    int fd = g_msl_tr_img->fd;
    if (size > SIZE_MAX - 1u) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        errno = EOVERFLOW;
        return 0;
    }
    g_msl_tr_inflight++;
    pthread_mutex_unlock(&g_msl_tr_mu);

    char *buf = malloc((size_t)size + 1u);
    int ok = 0, saved_errno = ENOMEM;
    if (buf) {
        if (!size || romfs_image_read(fd, buf, (size_t)size, (off_t)abs_off)) {
            buf[size] = '\0';
            *out_buf = buf;
            *out_len = (size_t)size;
            ok = 1;
        } else {
            saved_errno = errno ? errno : EIO;
            free(buf);
        }
    }

    pthread_mutex_lock(&g_msl_tr_mu);
    if (--g_msl_tr_inflight == 0) pthread_cond_broadcast(&g_msl_tr_cv);
    pthread_mutex_unlock(&g_msl_tr_mu);

    if (!ok) errno = saved_errno;
    return ok;
}

__attribute__((visibility("hidden"))) int dm_msl_trailer_map(const char *rel_path, msl_map *out) {
    if (!rel_path || !out) { errno = EINVAL; return 0; }
    memset(out, 0, sizeof *out);
    pthread_mutex_lock(&g_msl_tr_mu);
    if (!msl_tr_ensure_locked()) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        return 0;
    }
    const img_ent *x = msl_tr_resolve_locked(rel_path);
    if (!x) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        errno = ENOENT;
        return 0;
    }
    uint64_t abs_off = g_msl_tr_footer.msl_offset + x->a;
    uint64_t size = x->b;
    int fd = g_msl_tr_img->fd;
    if (!size) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        errno = EINVAL;
        return 0;
    }
    uint64_t page = (uint64_t)getpagesize();
    uint64_t delta = abs_off % page;
    if (size > SIZE_MAX - (size_t)delta) {
        pthread_mutex_unlock(&g_msl_tr_mu);
        errno = EOVERFLOW;
        return 0;
    }
    g_msl_tr_inflight++;
    pthread_mutex_unlock(&g_msl_tr_mu);

    size_t map_len = (size_t)size + (size_t)delta;
    void *p = mmap(NULL, map_len, PROT_READ, MAP_SHARED, fd, (off_t)(abs_off - delta));
    int ok = 0, saved_errno = 0;
    if (p != MAP_FAILED) {
        out->base = p;
        out->base_len = map_len;
        out->data = (const char *)p + delta;
        out->size = (size_t)size;
        ok = 1;
    } else {
        saved_errno = errno ? errno : ENOMEM;
    }

    pthread_mutex_lock(&g_msl_tr_mu);
    if (--g_msl_tr_inflight == 0) pthread_cond_broadcast(&g_msl_tr_cv);
    pthread_mutex_unlock(&g_msl_tr_mu);

    if (!ok) errno = saved_errno;
    return ok;
}

__attribute__((visibility("hidden"))) void dm_msl_trailer_teardown(void) {
    pthread_mutex_lock(&g_msl_tr_mu);
    msl_tr_reset_locked();
    pthread_mutex_unlock(&g_msl_tr_mu);
}
