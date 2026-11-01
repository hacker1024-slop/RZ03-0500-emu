// SPDX-License-Identifier: MIT
// Query the emulated BlackWidow V4 75% using an external original Razer DLL.
// Build as a 32-bit Windows program to match FWUpdaterDLL.dll.
#include <windows.h>
#include <stdio.h>

typedef HANDLE (__cdecl *open_fn)(ULONG, ULONG, USHORT, float, int, int, int, int);
typedef int (__cdecl *version_fn)(HANDLE, unsigned char *, unsigned char);
typedef void (__cdecl *close_fn)(HANDLE);

int main(int argc, char **argv)
{
    if (argc > 2) {
        fprintf(stderr, "Usage: razer_updater_query.exe [path-to-FWUpdaterDLL.dll]\n");
        return 1;
    }
    HMODULE library = LoadLibraryA(argc == 2 ? argv[1] : "FWUpdaterDLL.dll");
    if (!library) {
        fprintf(stderr, "Cannot load Razer DLL (Windows error %lu). Use a 32-bit build.\n",
                GetLastError());
        return 1;
    }
    open_fn open_device = (open_fn)GetProcAddress(library, "OpenDevice");
    version_fn get_version = (version_fn)GetProcAddress(library, "GetFWVersion");
    close_fn close_device = (close_fn)GetProcAddress(library, "CloseDevice");
    if (!open_device || !get_version || !close_device) {
        fprintf(stderr, "DLL does not export the expected Razer updater functions.\n");
        FreeLibrary(library);
        return 1;
    }
    // Parameters come from the original updater's device metadata:
    // VID/PID, no revision/bootloader filter, report type 4, feature length 91.
    HANDLE device = open_device(0x1532, 0x02A5, 0, 0.0f, 4, 91, 0, 0);
    if (!device || device == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "Razer DLL could not open the 1532:02A5 feature interface.\n");
        FreeLibrary(library);
        return 1;
    }
    unsigned char version[9] = {0};
    int status = get_version(device, version, 2); // Device type 2: keyboard.
    close_device(device);
    FreeLibrary(library);
    if (status != 2) {
        fprintf(stderr, "Razer firmware-version query failed (status %d).\n", status);
        return 1;
    }
    printf("Firmware version: %u.%u.%u\n", version[0], version[1], version[2]);
    return 0;
}
