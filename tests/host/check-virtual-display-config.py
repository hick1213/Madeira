#!/usr/bin/env python3
"""Execute production CCD enumeration and device-info functions with Wine ABI.

Only display locking, the Metal adapter identity, and physical-source settings
are host boundaries. Virtual current/registry state, modes, paths, names, and
validation run production C.
The virtual fixture has no source, exactly like Madeira's iOS display cache.
"""

from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "build/win32u-unix/sysparams_ios.c"
SCRATCH_PARENT = ROOT / "app/Build/Products/IPAInspection/future-display-probes"


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    while source.index(";", start) < source.index("{", start):
        start = source.index(signature, start + len(signature))
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


PRELUDE = r'''
#include <stddef.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <windef.h>
#include <winbase.h>
#include <wingdi.h>
#include <winuser.h>
#include <winternl.h>
#include <ntstatus.h>
#include <d3dkmdt.h>
#include <wine/list.h>
#include "madeira_kmt.h"
#define WINE_IOS 1
#define TRACE(...) ((void)0)
#define FIXME(...) ((void)0)
#define ERR(...) ((void)0)
#define WINE_ENUM_PHYSICAL_SETTINGS ((DWORD)-3)
#define VIRTUAL_HMONITOR ((HMONITOR)(UINT_PTR)0x10001)
static UINT checks;
static void require(BOOL ok, const char *message)
{
    if (!ok) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
    checks++;
}
static void asciiz_to_unicode(WCHAR *dst, const char *src)
{ do { *dst++ = (unsigned char)*src; } while (*src++); }
static WCHAR *fixture_wcscpy(WCHAR *dst, const WCHAR *src)
{ WCHAR *out = dst; do { *dst++ = *src; } while (*src++); return out; }
#define wcscpy fixture_wcscpy
static BOOL name_is(const WCHAR *actual, const char *expected)
{ while (*actual && *expected && *actual == (unsigned char)*expected) { actual++; expected++; } return !*actual && !*expected; }
'''


BOUNDARIES = r'''
static struct list sources = LIST_INIT(sources), monitors = LIST_INIT(monitors);
static struct monitor virtual_monitor = {.handle = VIRTUAL_HMONITOR};
static BOOL can_lock = TRUE, have_luid = TRUE, have_mode = TRUE;
static unsigned int locks;
static int screen_width = 1280, screen_height = 720;
static LUID adapter_luid = {0x12345678, 0x11223344};
static BOOL lock_display_devices(BOOL force)
{ (void)force; if (!can_lock) return FALSE; require(!locks, "no recursive display lock"); locks++; return TRUE; }
static void unlock_display_devices(void)
{ require(locks == 1, "balanced display unlock"); locks--; }
static BOOL ios_mode_at_index(UINT index, int *w, int *h)
{ (void)index; (void)w; (void)h; return FALSE; }
void WINAPI RtlSetLastWin32Error(ULONG error) { (void)error; }
static BOOL source_get_current_settings(struct source *source, DEVMODEW *mode)
{ *mode = source->current; return have_mode; }
int madeira_kmt_adapter_luid(LUID *luid)
{ if (have_luid) *luid = adapter_luid; return have_luid; }
void madeira_kmt_identity(struct madeira_kmt_identity *id)
{ memset(id, 0, sizeof(*id)); strcpy(id->path, "PCI\\VEN_106B&DEV_0001&SUBSYS_00000000&REV_00\\00000000"); }
static BOOL ios_virtual_enum_display_settings(DWORD index, DEVMODEW *mode, DWORD flags);
'''


def production_code() -> str:
    source = PRODUCTION.read_text()
    structs = source[source.index("struct gpu\n{") : source.index("static struct list gpus")]
    constants = "\n".join(
        line for line in source.splitlines()
        if line.startswith("static const UINT32 qdc_retrieve_flags_mask")
        or line.startswith("static const char guid_devinterface_")
    )
    state_start = source.index("static int ios_screen_cur_w, ios_screen_cur_h;")
    state_end = source.index("static void ios_screen_size(", state_start)
    functions = [
        "static BOOL is_detached_mode(",
        "static BOOL is_monitor_active(",
        "static void ios_screen_size(",
        "static BOOL ios_virtual_enum_display_settings(",
        "LONG WINAPI NtUserGetDisplayConfigBufferSizes(",
        "static DISPLAYCONFIG_ROTATION get_dc_rotation(",
        "static DISPLAYCONFIG_SCANLINE_ORDERING get_dc_scanline_ordering(",
        "static DISPLAYCONFIG_PIXELFORMAT get_dc_pixelformat(",
        "static void set_mode_target_info(",
        "static void set_path_target_info(",
        "static void set_mode_source_info(",
        "static void set_mode_desktop_info(",
        "static void set_path_source_info(",
        "static BOOL source_mode_exists(",
        "LONG WINAPI NtUserQueryDisplayConfig(",
        "static void monitor_get_interface_name(",
    ]
    if "static BOOL ios_virtual_display_device_info(" in source:
        functions.append("static BOOL ios_virtual_display_device_info(")
    functions.append("NTSTATUS WINAPI NtUserDisplayConfigGetDeviceInfo(")
    return structs + constants + BOUNDARIES + source[state_start:state_end] + "\n".join(
        function(source, sig) for sig in functions
    )


DRIVER = r'''
static BOOL same_luid(LUID a, LUID b)
{ return a.LowPart == b.LowPart && a.HighPart == b.HighPart; }
static struct gpu gpu;
static struct source source;
static struct monitor physical[2];
static void setup(BOOL use_virtual, BOOL clone)
{
    list_init(&sources); list_init(&monitors);
    if (use_virtual) { list_add_tail(&monitors, &virtual_monitor.entry); return; }
    memset(&gpu, 0, sizeof(gpu)); gpu.luid = (LUID){0x76543210, 0x44332211};
    strcpy(gpu.path, "PCI\\PHYSICAL\\0000");
    memset(&source, 0, sizeof(source));
    source.gpu = &gpu; source.id = 3;
    source.state_flags = DISPLAY_DEVICE_ATTACHED_TO_DESKTOP | DISPLAY_DEVICE_PRIMARY_DEVICE;
    source.current.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY;
    source.current.dmSize = sizeof(source.current);
    source.current.dmPelsWidth = 1920; source.current.dmPelsHeight = 1080;
    source.current.dmBitsPerPel = 32; source.current.dmDisplayFrequency = 75;
    source.modes = &source.current; source.mode_count = 1;
    list_add_tail(&sources, &source.entry);
    memset(physical, 0, sizeof(physical));
    physical[0].source = &source; physical[0].output_id = 7;
    list_add_tail(&monitors, &physical[0].entry);
    if (clone) {
        physical[1].source = &source; physical[1].output_id = 8; physical[1].id = 1;
        list_add_tail(&monitors, &physical[1].entry);
    }
}

static void verify_query(UINT32 flags, BOOL is_virtual, BOOL clone)
{
    struct { UINT64 before; DISPLAYCONFIG_PATH_INFO data[2]; UINT64 after; } p;
    struct { UINT64 before; DISPLAYCONFIG_MODE_INFO data[6]; UINT64 after; } m;
    DISPLAYCONFIG_TOPOLOGY_ID topology = 0;
    LUID expected = is_virtual ? adapter_luid : gpu.luid;
    UINT32 count = 0, modes = 0, i, buffer_count, buffer_modes;
    UINT32 expected_paths = clone ? 2 : 1;
    UINT32 expected_modes = 1 + expected_paths * ((flags & QDC_VIRTUAL_MODE_AWARE) ? 2 : 1);
    memset(&p, 0xcc, sizeof(p)); memset(&m, 0xcc, sizeof(m));
    p.before = p.after = m.before = m.after = 0x1122334455667788ull;
    require(NtUserGetDisplayConfigBufferSizes(flags, &count, &modes) == ERROR_SUCCESS, "buffer sizes succeed");
    require(count == expected_paths && modes >= expected_modes, "buffer sizes cover every active path and mode");
    buffer_count = count; buffer_modes = modes;
    require(NtUserQueryDisplayConfig(flags, &count, p.data, &modes, m.data,
            (flags & QDC_DATABASE_CURRENT) ? &topology : NULL) == ERROR_SUCCESS, "query succeeds");
    require(count == expected_paths && modes == expected_modes, "query actually fills reported active paths");
    require(count <= buffer_count && modes <= buffer_modes, "query respects allocated capacities");
    require(p.before == 0x1122334455667788ull && p.after == p.before &&
            m.before == p.before && m.after == p.before, "path and mode canaries intact");
    if (flags & QDC_DATABASE_CURRENT) require(topology == DISPLAYCONFIG_TOPOLOGY_INTERNAL, "database topology preserved");
    for (i = 0; i < count; i++) {
        DISPLAYCONFIG_PATH_INFO *path = &p.data[i];
        UINT32 si = (flags & QDC_VIRTUAL_MODE_AWARE) ? path->sourceInfo.sourceModeInfoIdx : path->sourceInfo.modeInfoIdx;
        UINT32 ti = (flags & QDC_VIRTUAL_MODE_AWARE) ? path->targetInfo.targetModeInfoIdx : path->targetInfo.modeInfoIdx;
        DISPLAYCONFIG_SOURCE_DEVICE_NAME name = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
            .header.size = sizeof(name), .header.adapterId = expected, .header.id = is_virtual ? 0 : source.id};
        UINT32 width = is_virtual ? screen_width : source.current.dmPelsWidth;
        UINT32 height = is_virtual ? screen_height : source.current.dmPelsHeight;
        require(path->flags & DISPLAYCONFIG_PATH_ACTIVE, "path active");
        require(path->targetInfo.targetAvailable && path->targetInfo.statusFlags == DISPLAYCONFIG_TARGET_IN_USE,
                "target available and in use");
        require(path->sourceInfo.statusFlags == DISPLAYCONFIG_SOURCE_IN_USE, "source in use");
        require(same_luid(path->sourceInfo.adapterId, expected) && same_luid(path->targetInfo.adapterId, expected),
                "path shares the existing adapter identity");
        require(si < modes && ti < modes && si != ti, "source and target mode indexes valid");
        require(m.data[si].infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE &&
                m.data[si].id == path->sourceInfo.id && same_luid(m.data[si].adapterId, expected), "source mode identity");
        require(m.data[ti].infoType == DISPLAYCONFIG_MODE_INFO_TYPE_TARGET &&
                m.data[ti].id == path->targetInfo.id && same_luid(m.data[ti].adapterId, expected), "target mode identity");
        require(m.data[si].sourceMode.width == width && m.data[si].sourceMode.height == height,
                "source mode has current display dimensions");
        require(m.data[ti].targetMode.targetVideoSignalInfo.activeSize.cx == width &&
                m.data[ti].targetMode.targetVideoSignalInfo.activeSize.cy == height, "target has current display dimensions");
        require(path->targetInfo.refreshRate.Numerator == (is_virtual ? 60 : 75) &&
                path->targetInfo.refreshRate.Denominator == 1, "current refresh rate retained");
        if (is_virtual) require(path->targetInfo.outputTechnology == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL,
                                "virtual path and target name share internal output technology");
        if (flags & QDC_VIRTUAL_MODE_AWARE) {
            UINT32 di = path->targetInfo.desktopModeInfoIdx;
            require(path->flags & DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE, "virtual-aware path marked");
            require(di < modes && di != si && di != ti && m.data[di].infoType == DISPLAYCONFIG_MODE_INFO_TYPE_DESKTOP_IMAGE,
                    "desktop mode index valid");
            require(m.data[di].desktopImageInfo.PathSourceSize.x == (LONG)width &&
                    m.data[di].desktopImageInfo.PathSourceSize.y == (LONG)height &&
                    m.data[di].desktopImageInfo.DesktopImageRegion.right == (LONG)width &&
                    m.data[di].desktopImageInfo.DesktopImageClip.bottom == (LONG)height, "desktop has current display dimensions");
        }
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_SUCCESS, "path source name query succeeds");
        require(name_is(name.viewGdiDeviceName, is_virtual ? "\\\\.\\DISPLAY1" : "\\\\.\\DISPLAY4"),
                "path source name matches GDI monitor name");
    }
}

static void verify_device_info(BOOL is_virtual)
{
    LUID luid = is_virtual ? adapter_luid : gpu.luid;
    UINT id = is_virtual ? 0 : 7;
    DISPLAYCONFIG_TARGET_DEVICE_NAME target = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME,
        .header.size = sizeof(target), .header.adapterId = luid, .header.id = id};
    DISPLAYCONFIG_TARGET_PREFERRED_MODE preferred = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_PREFERRED_MODE,
        .header.size = sizeof(preferred), .header.adapterId = luid, .header.id = id};
    DISPLAYCONFIG_ADAPTER_NAME adapter = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADAPTER_NAME,
        .header.size = sizeof(adapter), .header.adapterId = luid, .header.id = 0};
    DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO color = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO,
        .header.size = sizeof(color), .header.adapterId = luid, .header.id = id};
    require(NtUserDisplayConfigGetDeviceInfo(&target.header) == STATUS_SUCCESS, "target name query succeeds");
    require(name_is(target.monitorFriendlyDeviceName, is_virtual ? "Display1" : "Display8"), "target friendly name");
    require(target.outputTechnology == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL, "target output technology");
    if (is_virtual) require(name_is(target.monitorDevicePath,
        "\\\\?\\DISPLAY#Default_Monitor#4&madeira&0&UID0#{E6F07B5F-EE97-4A90-B076-33F57BF4EAA7}"), "existing monitor interface name");
    require(NtUserDisplayConfigGetDeviceInfo(&preferred.header) == STATUS_SUCCESS, "preferred mode query succeeds");
    require(preferred.width == (is_virtual ? (UINT)screen_width : 1920) &&
            preferred.height == (is_virtual ? (UINT)screen_height : 1080), "preferred mode current dimensions");
    require(NtUserDisplayConfigGetDeviceInfo(&adapter.header) == STATUS_SUCCESS, "adapter name query succeeds");
    require(name_is(adapter.adapterDevicePath, is_virtual
        ? "\\\\?\\PCI#VEN_106B&DEV_0001&SUBSYS_00000000&REV_00#00000000#{5B45201D-F2F2-4F3B-85BB-30FF1F953599}"
        : "\\\\?\\PCI#PHYSICAL#0000#{5B45201D-F2F2-4F3B-85BB-30FF1F953599}"), "adapter interface uses existing identity");
    require(NtUserDisplayConfigGetDeviceInfo(&color.header) == (is_virtual ? STATUS_UNSUCCESSFUL : STATUS_SUCCESS),
            "virtual unsupported color is not fabricated and physical color remains supported");
}

static void verify_packet_guards(void)
{
    union {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source;
        DISPLAYCONFIG_TARGET_DEVICE_NAME target;
        DISPLAYCONFIG_TARGET_PREFERRED_MODE preferred;
        DISPLAYCONFIG_ADAPTER_NAME adapter;
    } packet;
    const struct { DISPLAYCONFIG_DEVICE_INFO_TYPE type; UINT32 size; } kinds[] = {
        {DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, sizeof(packet.source)},
        {DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME, sizeof(packet.target)},
        {DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_PREFERRED_MODE, sizeof(packet.preferred)},
        {DISPLAYCONFIG_DEVICE_INFO_GET_ADAPTER_NAME, sizeof(packet.adapter)},
    };
    unsigned int i;
    for (i = 0; i < ARRAY_SIZE(kinds); i++) {
        DISPLAYCONFIG_DEVICE_INFO_HEADER *header = &packet.source.header;
        memset(&packet, 0xcc, sizeof(packet));
        *header = (DISPLAYCONFIG_DEVICE_INFO_HEADER){kinds[i].type, kinds[i].size, adapter_luid, 0};
        require(NtUserDisplayConfigGetDeviceInfo(header) == STATUS_SUCCESS, "supported packet succeeds");
        header->id = 1;
        require(NtUserDisplayConfigGetDeviceInfo(header) ==
                (kinds[i].type == DISPLAYCONFIG_DEVICE_INFO_GET_ADAPTER_NAME ? STATUS_SUCCESS : STATUS_UNSUCCESSFUL),
                "source and target ids are checked; adapter-name id is unused per Windows contract");
        header->id = 0; header->adapterId.LowPart++;
        require(NtUserDisplayConfigGetDeviceInfo(header) == STATUS_UNSUCCESSFUL, "wrong packet adapter identity rejected");
        header->size--;
        require(NtUserDisplayConfigGetDeviceInfo(header) == STATUS_INVALID_PARAMETER, "typed size check precedes identity lookup");
        header->size = sizeof(*header) - 1;
        require(NtUserDisplayConfigGetDeviceInfo(header) == STATUS_UNSUCCESSFUL, "truncated common packet header rejected");
    }
}

int main(int argc, char **argv)
{
    DISPLAYCONFIG_PATH_INFO paths[2] = {{0}};
    DISPLAYCONFIG_MODE_INFO modes[6] = {{0}};
    DISPLAYCONFIG_TOPOLOGY_ID topology = 0;
    DISPLAYCONFIG_SOURCE_DEVICE_NAME name = {.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
        .header.size = sizeof(name), .header.adapterId = adapter_luid, .header.id = 0};
    UINT32 pc = 1, mc = 2, flags = QDC_ONLY_ACTIVE_PATHS;
    const char *scenario;
    char screen_env[32];
    BOOL physical_case, clone;
    require(argc == 2, "one scenario argument"); scenario = argv[1];
    physical_case = !strncmp(scenario, "physical", 8);
    clone = !strcmp(scenario, "physical-clone");
    setup(!physical_case, clone);
    if (!strcmp(scenario, "virtual-aware")) flags |= QDC_VIRTUAL_MODE_AWARE;
    if (!strcmp(scenario, "all-paths")) flags = QDC_ALL_PATHS;
    if (!strcmp(scenario, "database")) flags = QDC_DATABASE_CURRENT;
    if (!strcmp(scenario, "all-paths-virtual-aware")) flags = QDC_ALL_PATHS | QDC_VIRTUAL_MODE_AWARE;
    if (!strcmp(scenario, "database-virtual-aware")) flags = QDC_DATABASE_CURRENT | QDC_VIRTUAL_MODE_AWARE;
    if (!strcmp(scenario, "changed-size")) { screen_width = 800; screen_height = 600; }
    snprintf(screen_env, sizeof(screen_env), "%d", screen_width);
    setenv("MADEIRA_SCREEN_W", screen_env, 1);
    snprintf(screen_env, sizeof(screen_env), "%d", screen_height);
    setenv("MADEIRA_SCREEN_H", screen_env, 1);
    if (!strcmp(scenario, "physical-clone")) flags |= QDC_VIRTUAL_MODE_AWARE;
    if (!strcmp(scenario, "source-wrong-luid")) name.header.adapterId.LowPart++;
    if (!strcmp(scenario, "source-wrong-id")) name.header.id++;
    if (!strcmp(scenario, "device-info-guards")) verify_packet_guards();
    else if (!strncmp(scenario, "source-wrong-", 13))
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_UNSUCCESSFUL, "wrong identity fails");
    else if (!strcmp(scenario, "source-short-header")) {
        name.header.size = sizeof(name.header) - 1;
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_UNSUCCESSFUL, "short common header fails");
    } else if (!strcmp(scenario, "source-short-packet")) {
        name.header.size = sizeof(name) - 1;
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_INVALID_PARAMETER, "short source packet fails");
    } else if (!strcmp(scenario, "unsupported-info")) {
        name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_BASE_TYPE;
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_INVALID_PARAMETER, "unsupported type remains failure");
    } else if (!strcmp(scenario, "null-info"))
        require(NtUserDisplayConfigGetDeviceInfo(NULL) == STATUS_UNSUCCESSFUL, "NULL packet fails");
    else if (!strcmp(scenario, "null-path-count"))
        require(NtUserQueryDisplayConfig(flags, NULL, paths, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "NULL path count fails");
    else if (!strcmp(scenario, "null-mode-count"))
        require(NtUserQueryDisplayConfig(flags, &pc, paths, NULL, modes, NULL) == ERROR_INVALID_PARAMETER, "NULL mode count fails");
    else if (!strcmp(scenario, "null-path-array"))
        require(NtUserQueryDisplayConfig(flags, &pc, NULL, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "NULL path array fails");
    else if (!strcmp(scenario, "null-mode-array"))
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, NULL, NULL) == ERROR_INVALID_PARAMETER, "NULL mode array fails");
    else if (!strcmp(scenario, "zero-path-count")) {
        pc = 0; require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "zero path capacity fails");
    } else if (!strcmp(scenario, "zero-mode-count")) {
        mc = 0; require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "zero mode capacity fails");
    } else if (!strcmp(scenario, "small-mode-buffer")) {
        mc = 1; require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_INSUFFICIENT_BUFFER, "one mode cannot fit source and target");
        require(!memcmp(&modes[1], &(DISPLAYCONFIG_MODE_INFO){0}, sizeof(modes[1])), "one-mode capacity does not write a second mode");
    } else if (!strcmp(scenario, "small-virtual-buffer")) {
        require(NtUserQueryDisplayConfig(flags | QDC_VIRTUAL_MODE_AWARE, &pc, paths, &mc, modes, NULL) == ERROR_INSUFFICIENT_BUFFER, "virtual path needs desktop mode too");
        require(!memcmp(&modes[2], &(DISPLAYCONFIG_MODE_INFO){0}, sizeof(modes[2])), "two-mode capacity does not write desktop mode");
    } else if (!strcmp(scenario, "unexpected-topology"))
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, &topology) == ERROR_INVALID_PARAMETER, "active query rejects topology pointer");
    else if (!strcmp(scenario, "missing-topology"))
        require(NtUserQueryDisplayConfig(QDC_DATABASE_CURRENT, &pc, paths, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "database needs topology pointer");
    else if (!strcmp(scenario, "invalid-flags") || !strcmp(scenario, "combined-retrieve-flags") || !strcmp(scenario, "no-retrieve-flags")) {
        flags = !strcmp(scenario, "invalid-flags") ? flags | 0x8
            : !strcmp(scenario, "combined-retrieve-flags") ? QDC_ALL_PATHS | QDC_ONLY_ACTIVE_PATHS : QDC_VIRTUAL_MODE_AWARE;
        require(NtUserGetDisplayConfigBufferSizes(flags, &pc, &mc) == ERROR_INVALID_PARAMETER, "invalid buffer flags fail");
        pc = 1; mc = 2;
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_INVALID_PARAMETER, "invalid query flags fail");
    } else if (!strcmp(scenario, "null-buffer-count"))
        require(NtUserGetDisplayConfigBufferSizes(flags, NULL, &mc) == ERROR_INVALID_PARAMETER, "NULL buffer count fails");
    else if (!strcmp(scenario, "null-buffer-mode-count"))
        require(NtUserGetDisplayConfigBufferSizes(flags, &pc, NULL) == ERROR_INVALID_PARAMETER, "NULL buffer mode count fails");
    else if (!strcmp(scenario, "lock-failure")) {
        can_lock = FALSE;
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_GEN_FAILURE, "display lock failure preserved");
    } else if (!strcmp(scenario, "no-adapter")) {
        have_luid = FALSE;
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_GEN_FAILURE, "no Metal adapter does not fabricate identity");
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_UNSUCCESSFUL, "no adapter has no source name");
    } else if (!strcmp(scenario, "absent-virtual-monitor")) {
        list_init(&monitors);
        require(NtUserDisplayConfigGetDeviceInfo(&name.header) == STATUS_UNSUCCESSFUL, "absent virtual monitor is not exposed");
    } else if (!strcmp(scenario, "physical-inactive")) {
        source.state_flags = 0;
        require(NtUserGetDisplayConfigBufferSizes(flags, &pc, &mc) == ERROR_SUCCESS && !pc && !mc, "inactive monitor does not count");
    } else if (!strcmp(scenario, "physical-mode-failure")) {
        have_mode = FALSE;
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_GEN_FAILURE, "physical current-mode failure retained");
    } else if (!strcmp(scenario, "physical-small-path-buffer")) {
        setup(FALSE, TRUE); mc = 6;
        require(NtUserQueryDisplayConfig(flags, &pc, paths, &mc, modes, NULL) == ERROR_INSUFFICIENT_BUFFER, "two physical targets need two path slots");
        require(!memcmp(&paths[1], &(DISPLAYCONFIG_PATH_INFO){0}, sizeof(paths[1])), "one-path capacity does not write a second path");
    } else {
        verify_query(flags, !physical_case, clone);
        verify_device_info(!physical_case);
    }
    require(!locks, "display lock released at return");
    printf("PASS: %s (%u checks)\n", scenario, checks);
    return 0;
}
'''


SCENARIOS = (
    "virtual-active", "virtual-aware", "all-paths", "database", "changed-size",
    "all-paths-virtual-aware", "database-virtual-aware", "device-info-guards",
    "physical-active", "physical-clone", "source-wrong-luid", "source-wrong-id",
    "source-short-header", "source-short-packet", "unsupported-info", "null-info",
    "null-path-count", "null-mode-count", "null-path-array", "null-mode-array",
    "zero-path-count", "zero-mode-count", "small-mode-buffer", "small-virtual-buffer",
    "unexpected-topology", "missing-topology", "invalid-flags", "combined-retrieve-flags",
    "no-retrieve-flags", "null-buffer-count", "null-buffer-mode-count", "lock-failure", "no-adapter",
    "absent-virtual-monitor", "physical-inactive", "physical-mode-failure", "physical-small-path-buffer",
)


def main() -> None:
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="host-display-config-", dir=SCRATCH_PARENT) as directory:
        c = Path(directory) / "check.c"
        c.write_text(PRELUDE + production_code() + DRIVER)
        for label, options in (
            ("asan-ubsan", ["-O1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]),
            ("optimized", ["-O2"]),
        ):
            binary = Path(directory) / label
            subprocess.run(
                ["cc", "-std=gnu11", "-D__WINESRC__", "-Wall", "-Wextra", "-Werror",
                 "-Wno-pragma-pack", "-Wno-unused-parameter", "-Wno-unused-function", "-Wno-unused-variable", *options,
                 "-I", str(ROOT / "wine/build-macos/include"), "-I", str(ROOT / "wine/include"),
                 "-I", str(ROOT / "build/win32u-unix"), str(c), "-o", str(binary)],
                check=True, timeout=30,
            )
            for scenario in SCENARIOS:
                result = subprocess.run([str(binary), scenario], text=True, capture_output=True, timeout=10)
                if result.returncode:
                    raise RuntimeError(f"{label} {scenario}:\n{result.stdout}{result.stderr}")
            print(f"PASS: {label} {len(SCENARIOS)} production display-config scenarios")


if __name__ == "__main__":
    main()
