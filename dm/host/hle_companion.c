// Guest memory access for an ACNH companion module (Eden Duo's ABI). The host owns the guest address space, so reads and the player's own inventory
// writes go through hle_svc.c's locked heap helpers rather than a bare vm_read: a concurrent SetHeapSize cannot turn a bounds check into a use-after-free.
#include "dm.h"
#include "companion.h"
#include <stdatomic.h>
#include <string.h>

static _Atomic int companion_ready;
static dm_companion_guest companion_guest;

uint64_t dm_companion_title_id(void) { return DM_GAME_PROGRAM_ID; }

void dm_companion_host_ready(void) {
    companion_guest.title_id = DM_GAME_PROGRAM_ID;
    for (int i = 0; i < g_nmod; i++) if (!strcmp(g_mod[i].name, "main")) {
        companion_guest.main_base = (uintptr_t)g_mod[i].base;
        companion_guest.main_size = g_mod[i].size;
        strlcpy(companion_guest.build_id, g_mod[i].build_id, sizeof companion_guest.build_id);
        break;
    }
    atomic_store_explicit(&companion_ready, 1, memory_order_release);
}

int dm_companion_guest_info(dm_companion_guest *out) {
    if (!out) return 0;
    memset(out, 0, sizeof *out);
    if (!atomic_load_explicit(&companion_ready, memory_order_acquire)) return 0;
    *out = companion_guest;
    uint64_t base; size_t open_len;
    dm_guest_heap_range(&base, &open_len);
    out->heap_begin = base;
    out->heap_end = base + open_len;
    return out->main_base && out->main_size;
}

int dm_companion_guest_mapped(uint64_t at, size_t n) {
    if (!at || !n || n > UINT64_MAX - at) return 0;
    dm_companion_guest g;
    if (!dm_companion_guest_info(&g)) return 0;
    if (at >= g.heap_begin && at <= g.heap_end && n <= g.heap_end - at) return 1;
    return at >= g.main_base && at - g.main_base <= g.main_size && n <= g.main_size - (at - g.main_base);
}

// The main module's image is outside the heap: read-only and pinned for the run, so a plain copy is safe there.
static int read_main_image(uint64_t at, void *out, size_t n) {
    dm_companion_guest g;
    if (!dm_companion_guest_info(&g) || at < g.main_base || at - g.main_base > g.main_size || n > g.main_size - (at - g.main_base)) return 0;
    memcpy(out, (void *)(uintptr_t)at, n);
    return 1;
}

// hle_svc.c's readers answer 0 on success, the ABI's bool wants nonzero.
int dm_companion_guest_read(uint64_t at, void *out, size_t n) {
    if (!out || !dm_companion_guest_mapped(at, n)) return 0;
    if (dm_guest_heap_read(at, out, n) == 0) return 1;
    return read_main_image(at, out, n);
}

typedef struct {
    uint64_t address; uint32_t size; uint8_t expect[64], value[64], before[64];
} companion_store;
typedef struct { companion_store ops[32]; uint32_t count; } companion_batch;

static int apply_batch(void *context) {
    companion_batch *b = context;
    // The guest threads are suspended here: re-read each target and compare it with the value the caller expected, so a change
    // between the early check in dm_companion_guest_write_batch and this point is refused rather than overwritten. The readability
    // test is lock-free (hle_svc.c), so it cannot deadlock on a suspended guest thread that holds the mapping lock; the heap cannot
    // be resized while the guest is stopped.
    // Every target is checked before any is written: a refused batch leaves the heap untouched, as the batch is all-or-nothing.
    // The caller refuses overlapping ranges, so the checks still hold while the second pass writes.
    for (uint32_t i = 0; i < b->count; i++) {
        companion_store *p = &b->ops[i];
        uint8_t *out = (uint8_t *)(uintptr_t)p->address;
        if (!dm_guest_heap_readable(p->address, p->size)) return 0;
        memcpy(p->before, out, p->size);
        if (memcmp(p->before, p->expect, p->size)) return 0;
    }
    for (uint32_t i = 0; i < b->count; i++) {
        companion_store *p = &b->ops[i];
        uint8_t *out = (uint8_t *)(uintptr_t)p->address;
        dm_guest_write_prepare(out, p->size);
        memcpy(out, p->value, p->size);
    }
    return 1;
}

int dm_companion_guest_write_batch(const dm_companion_write_op *ops, uint32_t count) {
    if (!ops || !count || count > 32) return 0;
    companion_batch batch = {.count = count};
    for (uint32_t i = 0; i < count; i++) {
        const dm_companion_write_op *p = &ops[i];
        if (!p->size || p->size > 64 || !p->expect || !p->value || p->address > UINT64_MAX - p->size) return 0;
        if (!dm_companion_guest_mapped(p->address, p->size)) return 0;
        if (!dm_guest_heap_readable(p->address, p->size)) return 0;
        if (dm_guest_heap_read(p->address, batch.ops[i].before, p->size) != 0) return 0;
        if (memcmp(batch.ops[i].before, p->expect, p->size)) return 0;
        for (uint32_t j = 0; j < i; j++)
            if (p->address < batch.ops[j].address + batch.ops[j].size && batch.ops[j].address < p->address + p->size) return 0;
        batch.ops[i].address = p->address; batch.ops[i].size = p->size;
        memcpy(batch.ops[i].expect, p->expect, p->size);
        memcpy(batch.ops[i].value, p->value, p->size);
    }
    return dm_guest_transaction(apply_batch, &batch);
}
