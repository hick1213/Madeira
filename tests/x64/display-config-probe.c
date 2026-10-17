/* Display enumeration consistency: active CCD paths must name monitors that
 * GetMonitorInfo reports, with valid modes and positive dimensions. */
#define _WIN32_WINNT 0x0a00
#include <windows.h>
#include <stdio.h>
#include <string.h>

#define PROBE_MAX_MONITORS 16

static MONITORINFOEXA monitors[PROBE_MAX_MONITORS];
static unsigned int monitor_count, failures;

static void check(BOOL ok, const char *contract)
{
    char line[256];
    snprintf(line, sizeof(line), "DISPLAY-CONFIG %s %s\n", ok ? "PASS" : "FAIL", contract);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
    if (!ok) failures++;
}

static BOOL CALLBACK enum_monitor(HMONITOR monitor, HDC dc, RECT *rect, LPARAM param)
{
    MONITORINFOEXA info = {0};
    char line[256];
    (void)dc;
    (void)rect;
    (void)param;
    info.cbSize = sizeof(info);
    if (!GetMonitorInfoA(monitor, (MONITORINFO *)&info)) {
        check(FALSE, "GetMonitorInfoA");
        return TRUE;
    }
    check(TRUE, "GetMonitorInfoA");
    if (!memchr(info.szDevice, 0, sizeof(info.szDevice))) {
        check(FALSE, "bounded monitor device name");
        return TRUE;
    }
    check(info.rcMonitor.right > info.rcMonitor.left &&
          info.rcMonitor.bottom > info.rcMonitor.top, "positive monitor rectangle");
    snprintf(line, sizeof(line), "DISPLAY-CONFIG monitor=%s rect=%ld,%ld-%ld,%ld\n",
             info.szDevice, info.rcMonitor.left, info.rcMonitor.top,
             info.rcMonitor.right, info.rcMonitor.bottom);
    OutputDebugStringA(line);
    fputs(line, stderr);
    if (monitor_count == PROBE_MAX_MONITORS) {
        check(FALSE, "monitor capacity");
        return FALSE;
    }
    monitors[monitor_count++] = info;
    return TRUE;
}

static BOOL same_luid(LUID a, LUID b)
{
    return a.LowPart == b.LowPart && a.HighPart == b.HighPart;
}

static void query_config(UINT32 flags)
{
    DISPLAYCONFIG_PATH_INFO paths[PROBE_MAX_MONITORS];
    DISPLAYCONFIG_MODE_INFO modes[PROBE_MAX_MONITORS * 3];
    DISPLAYCONFIG_TOPOLOGY_ID topology = 0;
    UINT32 path_count = 0, mode_count = 0, i;
    LONG error = GetDisplayConfigBufferSizes(flags, &path_count, &mode_count);
    char line[160];
    snprintf(line, sizeof(line), "DISPLAY-CONFIG buffers flags=%#x error=%ld paths=%u modes=%u\n",
             flags, error, path_count, mode_count);
    OutputDebugStringA(line);
    fputs(line, stderr);
    check(error == ERROR_SUCCESS, "buffer query success");
    check(path_count > 0 && path_count <= PROBE_MAX_MONITORS &&
          mode_count > 0 && mode_count <= PROBE_MAX_MONITORS * 3,
          "buffer counts describe active display");
    if (error || !path_count || !mode_count || path_count > PROBE_MAX_MONITORS ||
        mode_count > PROBE_MAX_MONITORS * 3) return;
    memset(paths, 0xcc, sizeof(paths));
    memset(modes, 0xcc, sizeof(modes));
    error = QueryDisplayConfig(flags, &path_count, paths, &mode_count, modes,
                              (flags & QDC_DATABASE_CURRENT) ? &topology : NULL);
    snprintf(line, sizeof(line), "DISPLAY-CONFIG query flags=%#x error=%ld paths=%u modes=%u\n",
             flags, error, path_count, mode_count);
    OutputDebugStringA(line);
    fputs(line, stderr);
    check(error == ERROR_SUCCESS, "path query success");
    check(path_count > 0 && path_count <= PROBE_MAX_MONITORS && mode_count <= PROBE_MAX_MONITORS * 3,
          "query returns active paths");
    if (error || path_count > PROBE_MAX_MONITORS || mode_count > PROBE_MAX_MONITORS * 3) return;
    for (i = 0; i < path_count; i++) {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME name = {0};
        UINT32 source_index, target_index;
        char gdi_name[CCHDEVICENAME] = {0};
        unsigned int j;
        BOOL matched = FALSE;
        DISPLAYCONFIG_MODE_INFO *source, *target;
        if (!(paths[i].flags & DISPLAYCONFIG_PATH_ACTIVE)) {
            check((flags & QDC_ALL_PATHS) != 0, "inactive path only in all-path query");
            continue;
        }
        check(paths[i].targetInfo.targetAvailable, "target available");
        source_index = (flags & QDC_VIRTUAL_MODE_AWARE)
                       ? paths[i].sourceInfo.sourceModeInfoIdx : paths[i].sourceInfo.modeInfoIdx;
        target_index = (flags & QDC_VIRTUAL_MODE_AWARE)
                       ? paths[i].targetInfo.targetModeInfoIdx : paths[i].targetInfo.modeInfoIdx;
        check(source_index < mode_count && target_index < mode_count, "valid mode indexes");
        if (source_index >= mode_count || target_index >= mode_count) continue;
        source = &modes[source_index];
        target = &modes[target_index];
        check(source->infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE &&
              source->id == paths[i].sourceInfo.id &&
              same_luid(source->adapterId, paths[i].sourceInfo.adapterId), "source mode identity");
        check(target->infoType == DISPLAYCONFIG_MODE_INFO_TYPE_TARGET &&
              target->id == paths[i].targetInfo.id &&
              same_luid(target->adapterId, paths[i].targetInfo.adapterId), "target mode identity");
        check(same_luid(paths[i].sourceInfo.adapterId, paths[i].targetInfo.adapterId) &&
              (paths[i].sourceInfo.adapterId.LowPart || paths[i].sourceInfo.adapterId.HighPart),
              "one nonzero adapter identity");
        check(source->sourceMode.width && source->sourceMode.height &&
              target->targetMode.targetVideoSignalInfo.activeSize.cx &&
              target->targetMode.targetVideoSignalInfo.activeSize.cy, "positive mode dimensions");
        if (flags & QDC_VIRTUAL_MODE_AWARE) {
            UINT32 desktop_index = paths[i].targetInfo.desktopModeInfoIdx;
            check((paths[i].flags & DISPLAYCONFIG_PATH_SUPPORT_VIRTUAL_MODE) &&
                  desktop_index < mode_count, "virtual desktop mode index");
            if (desktop_index < mode_count) {
                /* mingw's mode union omits the desktopImageInfo member; its
                 * SDK type shares the same union offset and fits targetMode. */
                const DISPLAYCONFIG_DESKTOP_IMAGE_INFO *desktop =
                    (const void *)&modes[desktop_index].targetMode;
                _Static_assert(sizeof(DISPLAYCONFIG_DESKTOP_IMAGE_INFO) <= sizeof(DISPLAYCONFIG_TARGET_MODE),
                               "desktop mode fits the CCD mode union");
                check(modes[desktop_index].infoType == DISPLAYCONFIG_MODE_INFO_TYPE_DESKTOP_IMAGE &&
                      desktop->PathSourceSize.x == (LONG)source->sourceMode.width &&
                      desktop->PathSourceSize.y == (LONG)source->sourceMode.height,
                      "virtual desktop mode dimensions");
            }
        }
        name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
        name.header.size = sizeof(name);
        name.header.adapterId = paths[i].sourceInfo.adapterId;
        name.header.id = paths[i].sourceInfo.id;
        error = DisplayConfigGetDeviceInfo(&name.header);
        check(error == ERROR_SUCCESS, "source device name query");
        if (error) continue;
        for (j = 0; j < CCHDEVICENAME && name.viewGdiDeviceName[j]; j++) {}
        if (j == CCHDEVICENAME) {
            check(FALSE, "bounded source device name");
            continue;
        }
        if (!WideCharToMultiByte(CP_ACP, 0, name.viewGdiDeviceName, -1,
                                 gdi_name, sizeof(gdi_name), NULL, NULL)) {
            check(FALSE, "source device name conversion");
            continue;
        }
        check(TRUE, "source device name conversion");
        for (j = 0; j < monitor_count; j++) {
            if (strcmp(monitors[j].szDevice, gdi_name)) continue;
            matched = TRUE;
            check(source->sourceMode.width == (UINT32)(monitors[j].rcMonitor.right - monitors[j].rcMonitor.left) &&
                  source->sourceMode.height == (UINT32)(monitors[j].rcMonitor.bottom - monitors[j].rcMonitor.top),
                  "source dimensions match GetMonitorInfo");
        }
        check(matched, "source name matches GetMonitorInfo");
    }
}

int main(void)
{
    SetProcessDPIAware();
    check(EnumDisplayMonitors(NULL, NULL, enum_monitor, 0), "EnumDisplayMonitors");
    check(monitor_count > 0, "at least one monitor");
    query_config(QDC_ONLY_ACTIVE_PATHS);
    query_config(QDC_ONLY_ACTIVE_PATHS | QDC_VIRTUAL_MODE_AWARE);
    query_config(QDC_ALL_PATHS);
    query_config(QDC_DATABASE_CURRENT);
    check(!failures, "terminal display enumeration consistency");
    return failures ? 1 : 0;
}
