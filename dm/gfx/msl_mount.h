// Zero-copy reader and page-aligned mapper for the optional DMMSLTR1 shader trailer appended to DM_ROMFS.
#pragma once
#include <stddef.h>
#include "../host/romfs_image.h"
#include "nvn/msl_image.h"

#ifdef __cplusplus
extern "C" {
#endif

// 1 when DM_ROMFS is a valid image carrying a DMMSLTR1 trailer; fills *out_footer when non-NULL.
__attribute__((visibility("hidden"))) int dm_msl_trailer_info(dm_msl_footer *out_footer);

// 1 when rel_path exists as a file in the mounted DM_ROMFS shader trailer (applying DM_PROGRAM fallback when multi_program is set).
__attribute__((visibility("hidden"))) int dm_msl_trailer_has(const char *rel_path);

// Reads rel_path into a NUL-terminated malloc buffer (*out_buf of *out_len + 1 bytes; caller frees *out_buf). 1 on success, 0 on miss or error.
__attribute__((visibility("hidden"))) int dm_msl_trailer_read(const char *rel_path, void **out_buf, size_t *out_len);

// Maps rel_path read-only with MAP_SHARED aligned down to getpagesize(). Caller munmaps (out->base, out->base_len). 1 on success, 0 on miss or error.
__attribute__((visibility("hidden"))) int dm_msl_trailer_map(const char *rel_path, msl_map *out);

// Closes the cached DM_ROMFS descriptor, frees the trailer index and resets state (called at gfx and shell teardown).
__attribute__((visibility("hidden"))) void dm_msl_trailer_teardown(void);

#ifdef __cplusplus
}
#endif
