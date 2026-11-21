// Portable boundary between a companion provider and the native guest host.
// The reader is read-only; the write batch is the player's own inventory moves,
// expect-guarded and stopped while the guest threads are suspended.
#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct {
    uint64_t title_id, main_base, main_size, heap_begin, heap_end;
    char build_id[33];
} dm_companion_guest;
void dm_companion_host_ready(void);
uint64_t dm_companion_title_id(void);
int dm_companion_guest_info(dm_companion_guest *out);
int dm_companion_guest_mapped(uint64_t address, size_t size);
int dm_companion_guest_read(uint64_t address, void *out, size_t size);
// A file in the game's RomFS: DM_ROMFS is an unpacked folder or a single DMROMFS1 image, and the reader resolves both.
// out == NULL && n == 0 asks for the size; otherwise it reads n bytes at offset. Returns the size or the number of bytes read.
size_t dm_companion_read_romfs(const char *path, uint64_t offset, void *out, size_t n);
typedef struct { uint64_t address; uint32_t size; const void *expect, *value; } dm_companion_write_op;
// At most 32 nonoverlapping, expect-guarded writes of 1..64 bytes to writable heap data.
int dm_companion_guest_write_batch(const dm_companion_write_op *ops, uint32_t count);
void dm_companion_input(uint64_t buttons);   // hle_hid.c: the companion's short A / D-pad presses for its item-menu drive
void dm_companion_input_reset(void);
#ifdef __cplusplus
}
#endif
