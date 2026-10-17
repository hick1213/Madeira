#import <Foundation/Foundation.h>

// Call only while this prefix has no running wineserver. Resolve trusted sandbox
// ancestry before constructing prefix_path; every supplied path component is
// opened without following symlinks. resource_path names the bundled cjk-fonts dir.
BOOL madeira_seed_cjk_fonts(const char *prefix_path, const char *resource_path,
                            const char *expected_sha256, NSError **error);
