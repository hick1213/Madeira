#import "RuntimeFonts.h"
#import <CommonCrypto/CommonDigest.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>

static NSString *const FontFile = @"NotoSansCJKsc-Regular.otf";
static NSString *const FontFamily = @"Noto Sans CJK SC (TrueType)";
static NSString *const FontLink = @"NotoSansCJKsc-Regular.otf,Noto Sans CJK SC";
static const size_t HiveLimit = 16 * 1024 * 1024;
static const off_t FontLimit = 64 * 1024 * 1024;

static BOOL Fail(NSError **error, NSString *message)
{
    if (error) *error = [NSError errorWithDomain:@"MadeiraRuntimeFonts" code:1
                                      userInfo:@{NSLocalizedDescriptionKey: message}];
    return NO;
}

typedef NS_ENUM(NSUInteger, FileAccessStep) {
    OpenContainer, OpenDirectory, CreateDirectory, OpenResource, OpenSource, OpenRegistry, CreateTemporary
};

static BOOL FileAccessFail(NSError **error, FileAccessStep step)
{
    int code = errno;
    NSArray<NSString *> *steps = @[@"open-container", @"open-directory", @"create-directory",
                                  @"open-resource", @"open-source", @"open-registry", @"create-temporary"];
    BOOL result = Fail(error, [NSString stringWithFormat:@"CJK font file access failed (step=%@ errno=%d).", steps[step], code]);
    errno = code;
    return result;
}

static NSString *DirectoryPath(NSString *path)
{
    if (![path hasPrefix:@"/"]) return nil;
    // /var is a protected OS alias; writable descendant links remain untrusted.
    if ([path isEqualToString:@"/var"] || [path hasPrefix:@"/var/"])
        path = [@"/private" stringByAppendingString:path];
    NSArray<NSString *> *parts = [path componentsSeparatedByString:@"/"];
    if (parts.count > 65 || [parts containsObject:@"."] || [parts containsObject:@".."]) return nil;
    return path;
}

static int Directory(const char *path, NSError **error)
{
    NSString *target = path ? DirectoryPath([NSString stringWithUTF8String:path]) : nil;
    NSString *anchor = DirectoryPath(NSHomeDirectory());
    NSArray<NSString *> *targetParts = target.pathComponents, *anchorParts = anchor.pathComponents;
    if (!target || !anchor || targetParts.count < anchorParts.count
        || ![[targetParts subarrayWithRange:NSMakeRange(0, anchorParts.count)] isEqualToArray:anchorParts]) {
        errno = EINVAL; return -1;
    }
    int fd = open(anchor.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) { FileAccessFail(error, OpenContainer); return -1; }
    struct stat anchorInfo;
    if (fstat(fd, &anchorInfo) < 0 || !S_ISDIR(anchorInfo.st_mode)) {
        int code = errno; close(fd); errno = code; return -1;
    }
    for (NSUInteger i = anchorParts.count; i < targetParts.count; i++) {
        NSString *part = targetParts[i];
        int next = openat(fd, part.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        int code = errno;
        close(fd);
        fd = next;
        if (fd < 0) { errno = code; FileAccessFail(error, OpenDirectory); break; }
    }
    return fd;
}

static BOOL Regular(int fd, off_t limit, struct stat *info)
{
    return fd >= 0 && fstat(fd, info) == 0 && S_ISREG(info->st_mode)
        && info->st_size >= 0 && info->st_size <= limit;
}

static BOOL WriteAll(int fd, const void *bytes, size_t count)
{
    const char *cursor = bytes;
    while (count) {
        ssize_t n = write(fd, cursor, count);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return NO;
        cursor += n; count -= (size_t)n;
    }
    return YES;
}

static BOOL Matches(int fd, NSData *data)
{
    unsigned char buffer[65536];
    size_t offset = 0;
    while (offset < data.length) {
        ssize_t n = read(fd, buffer, MIN(sizeof(buffer), data.length - offset));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0 || memcmp(buffer, (const char *)data.bytes + offset, (size_t)n)) return NO;
        offset += (size_t)n;
    }
    return YES;
}

// Both hashing and copying stay bounded regardless of bundled font size.
static NSString *HashCopy(int source, int destination)
{
    if (lseek(source, 0, SEEK_SET) < 0) return nil;
    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    unsigned char buffer[65536];
    off_t total = 0;
    for (;;) {
        ssize_t n = read(source, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) return nil;
        if (!n) break;
        total += n;
        if (total > FontLimit || (destination >= 0 && !WriteAll(destination, buffer, (size_t)n))) return nil;
        CC_SHA256_Update(&ctx, buffer, (CC_LONG)n);
    }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &ctx);
    NSMutableString *hex = [NSMutableString stringWithCapacity:64];
    for (size_t i = 0; i < sizeof(digest); i++) [hex appendFormat:@"%02x", digest[i]];
    return hex;
}

static NSString *Trim(NSString *line)
{
    return [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
}

static BOOL Escape(NSString *text, NSUInteger *index, unichar *decoded)
{
    NSUInteger i = *index + 1;
    if (i >= text.length) return NO;
    unichar c = [text characterAtIndex:i];
    if (c == 'x' || (c >= '0' && c <= '7')) {
        BOOL hex = c == 'x';
        NSUInteger start = hex ? ++i : i, count = 0;
        unsigned value = 0;
        while (i < text.length && count < (hex ? 4U : 3U)) {
            unichar digit = [text characterAtIndex:i];
            unsigned n = digit >= '0' && digit <= '9' ? digit - '0'
                : digit >= 'a' && digit <= 'f' ? digit - 'a' + 10
                : digit >= 'A' && digit <= 'F' ? digit - 'A' + 10 : 16;
            if (n >= (hex ? 16U : 8U)) break;
            value = value * (hex ? 16U : 8U) + n; count++; i++;
        }
        if (i == start) return NO;
        *decoded = (unichar)value; *index = i - 1; return YES;
    }
    NSString *codes = @"abtnvfre";
    NSRange code = [codes rangeOfString:[NSString stringWithFormat:@"%C", c]];
    const unichar values[] = {7, 8, 9, 10, 11, 12, 13, 27};
    if (code.location != NSNotFound) c = values[code.location];
    else if (c != '\\' && c != '"' && c != '[' && c != ']') return NO;
    *decoded = c; *index = i; return YES;
}

static NSString *Section(NSString *line, NSUInteger *end)
{
    NSMutableString *name = [NSMutableString string];
    for (NSUInteger i = 1; i < line.length; i++) {
        unichar c = [line characterAtIndex:i];
        if (c == '\\') {
            if (!Escape(line, &i, &c) || !c) return nil;
        } else if (c == ']') {
            *end = i;
            return name.length ? name : nil;
        }
        [name appendFormat:@"%C", c];
    }
    return nil;
}

// Decode only the quoted key. Values remain in their original Wine text form.
static NSString *Key(NSString *line, NSUInteger *valueStart)
{
    if (![line hasPrefix:@"\""]) return nil;
    NSMutableString *key = [NSMutableString string];
    for (NSUInteger i = 1; i < line.length; i++) {
        unichar c = [line characterAtIndex:i];
        if (c == '\\') {
            if (!Escape(line, &i, &c) || !c) return nil;
        } else if (c == '"') {
            i++;
            while (i < line.length && [[NSCharacterSet whitespaceCharacterSet] characterIsMember:[line characterAtIndex:i]]) i++;
            if (i >= line.length || [line characterAtIndex:i] != '=') return nil;
            *valueStart = i + 1;
            return key;
        }
        [key appendFormat:@"%C", c];
    }
    return nil;
}

static NSString *AppendLink(NSString *value)
{
    if (![value hasPrefix:@"str(7):\""] || ![value hasSuffix:@"\""]) return nil;
    NSString *body = [value substringWithRange:NSMakeRange(8, value.length - 9)];
    // Wine omits the final implicit NUL; some imported hives explicitly keep it.
    NSMutableArray<NSString *> *entries = [NSMutableArray array];
    NSMutableString *entry = [NSMutableString string];
    NSUInteger terminators = 0;
    NSUInteger extraTerminatorStart = NSNotFound;
    BOOL lastNull = body.length == 0;
    for (NSUInteger i = 0; i < body.length; i++) {
        unichar c = [body characterAtIndex:i];
        if (c == '"' || c < 32) return nil;
        if (c == '\\') {
            NSUInteger escapeStart = i;
            if (!Escape(body, &i, &c)) return nil;
            if (c == 0) {
                lastNull = YES;
                if (entry.length) {
                    if (terminators) return nil;
                    [entries addObject:[entry copy]]; [entry setString:@""];
                } else { terminators++; extraTerminatorStart = escapeStart; }
                continue;
            }
        }
        if (terminators || c < 32) return nil;
        lastNull = NO;
        [entry appendFormat:@"%C", c];
    }
    if (entry.length || terminators > 1 || !lastNull) return nil;
    for (NSString *existing in entries)
        if ([existing caseInsensitiveCompare:FontLink] == NSOrderedSame) return value;
    NSString *suffix = terminators ? @"\\0" : @"";
    NSString *trimmed = terminators ? [body substringToIndex:extraTerminatorStart] : body;
    return [NSString stringWithFormat:@"str(7):\"%@%@\\0%@\"", trimmed, FontLink, suffix];
}

static NSData *Patch(NSData *data, NSError **error)
{
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (!text || (![text hasPrefix:@"WINE REGISTRY Version 2\n"]
        && ![text hasPrefix:@"WINE REGISTRY Version 2\r\n"])) {
        Fail(error, @"Invalid Wine system registry header or encoding."); return nil;
    }
    NSString *newline = [text containsString:@"\r\n"] ? @"\r" : @"";
    NSArray *sections = @[@"Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\Fonts",
                          @"Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\FontLink\\\\SystemLink"];
    NSArray *sectionIdentities = @[@"Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts",
                                   @"Software\\Microsoft\\Windows NT\\CurrentVersion\\FontLink\\SystemLink"];
    NSArray *families = @[@"Tahoma", @"Microsoft Sans Serif", @"Segoe UI"];
    NSMutableArray<NSString *> *lines = [[text componentsSeparatedByString:@"\n"] mutableCopy];
    NSMutableDictionary<NSNumber *, NSNumber *> *ends = [NSMutableDictionary dictionary];
    NSMutableSet<NSNumber *> *seenSections = [NSMutableSet set];
    NSMutableSet<NSString *> *seenKeys = [NSMutableSet set];
    NSMutableDictionary<NSNumber *, NSArray<NSString *> *> *insertions = [NSMutableDictionary dictionary];
    NSMutableArray<NSString *> *newSections = [NSMutableArray array];
    NSInteger section = -1;
    for (NSUInteger i = 1; i < lines.count; i++) {
        NSString *line = Trim(lines[i]);
        if ([line hasPrefix:@"["]) {
            NSUInteger end = 0;
            NSString *name = Section(line, &end);
            if (!name) goto malformed;
            if (section >= 0) ends[@(section)] = @(i);
            section = -1;
            for (NSUInteger s = 0; s < sections.count; s++) {
                if ([name caseInsensitiveCompare:sectionIdentities[s]] == NSOrderedSame) {
                    if ([seenSections containsObject:@(s)]) goto malformed;
                    [seenSections addObject:@(s)]; section = (NSInteger)s;
                }
            }
            continue;
        }
        if (section < 0 || ![line hasPrefix:@"\""]) continue;
        NSUInteger start = 0;
        NSString *key = Key(line, &start);
        if (!key) goto malformed;
        NSString *target = nil;
        if (section == 0 && [key caseInsensitiveCompare:FontFamily] == NSOrderedSame) target = FontFamily;
        if (section == 1) for (NSString *family in families)
            if ([key caseInsensitiveCompare:family] == NSOrderedSame) target = family;
        if (!target) continue;
        if ([seenKeys containsObject:target]) goto malformed;
        [seenKeys addObject:target];
        NSString *value = Trim([line substringFromIndex:start]);
        if (section == 0) {
            if ([value caseInsensitiveCompare:[NSString stringWithFormat:@"\"%@\"", FontFile]] != NSOrderedSame) goto malformed;
        } else {
            NSString *patched = AppendLink(value);
            if (!patched) goto malformed;
            if (![patched isEqualToString:value])
                lines[i] = [NSString stringWithFormat:@"%@%@%@", [line substringToIndex:start], patched, newline];
        }
    }
    if (section >= 0) ends[@(section)] = @(lines.count);
    for (NSUInteger s = 0; s < sections.count; s++) {
        NSMutableArray *additions = [NSMutableArray array];
        NSArray *keys = s == 0 ? @[FontFamily] : families;
        for (NSString *key in keys) if (![seenKeys containsObject:key]) {
            NSString *value = s == 0 ? [NSString stringWithFormat:@"\"%@\"", FontFile]
                : [NSString stringWithFormat:@"str(7):\"%@\\0\"", FontLink];
            [additions addObject:[NSString stringWithFormat:@"\"%@\"=%@%@", key, value, newline]];
        }
        if (!additions.count) continue;
        if (![seenSections containsObject:@(s)]) {
            [newSections addObject:newline];
            [newSections addObject:[NSString stringWithFormat:@"[%@]%@", sections[s], newline]];
            [newSections addObjectsFromArray:additions];
            [newSections addObject:@""];
        } else {
            insertions[ends[@(s)]] = additions;
        }
    }
    for (NSNumber *location in [[insertions allKeys] sortedArrayUsingComparator:^NSComparisonResult(NSNumber *a, NSNumber *b) {
        return [b compare:a];
    }]) {
        NSArray *additions = insertions[location];
        [lines insertObjects:additions atIndexes:[NSIndexSet indexSetWithIndexesInRange:NSMakeRange(location.unsignedIntegerValue, additions.count)]];
    }
    [lines addObjectsFromArray:newSections];
    return [[lines componentsJoinedByString:@"\n"] dataUsingEncoding:NSUTF8StringEncoding];
malformed:
    Fail(error, @"Malformed or conflicting CJK registry settings; prefix was retained.");
    return nil;
}

static int Temporary(int dir, NSString **name)
{
    *name = [@".madeira-cjk-" stringByAppendingString:NSUUID.UUID.UUIDString];
    return openat(dir, (*name).fileSystemRepresentation,
                  O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
}

BOOL madeira_seed_cjk_fonts(const char *prefix_path, const char *resource_path,
                            const char *expected_sha256, NSError **error)
{
    if (error) *error = nil;
    NSString *expected = expected_sha256 ? [NSString stringWithUTF8String:expected_sha256] : nil;
    NSCharacterSet *hex = [NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"];
    if (expected.length != 64 || [expected rangeOfCharacterFromSet:hex.invertedSet].location != NSNotFound)
        return Fail(error, @"Invalid pinned CJK font hash.");
    int prefix = -1, drive = -1, windows = -1, fonts = -1, resources = -1;
    int source = -1, existing = -1, registry = -1, backup = -1, backupTemp = -1, fontTemp = -1, regTemp = -1;
    NSString *fontName = nil, *regName = nil, *backupTempName = nil;
    BOOL ok = NO, installed = NO;
    struct stat sourceInfo, regInfo, other;
    prefix = Directory(prefix_path, error);
    if (prefix < 0) goto failed;
    drive = openat(prefix, "drive_c", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (drive < 0) { FileAccessFail(error, OpenDirectory); goto failed; }
    windows = openat(drive, "windows", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (windows < 0) { FileAccessFail(error, OpenDirectory); goto failed; }
    fonts = openat(windows, "Fonts", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fonts < 0 && errno == ENOENT) {
        if (mkdirat(windows, "Fonts", 0755) < 0) { FileAccessFail(error, CreateDirectory); goto failed; }
        fonts = openat(windows, "Fonts", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    }
    if (fonts < 0) { FileAccessFail(error, OpenDirectory); goto failed; }
    resources = resource_path ? open(resource_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC) : -1;
    if (resources < 0) { FileAccessFail(error, OpenResource); goto failed; }
    source = openat(resources, FontFile.fileSystemRepresentation, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (source < 0) { FileAccessFail(error, OpenSource); goto failed; }
    registry = openat(prefix, "system.reg", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (registry < 0) { FileAccessFail(error, OpenRegistry); goto failed; }
    if (!Regular(source, FontLimit, &sourceInfo) || sourceInfo.st_size == 0
        || !Regular(registry, HiveLimit, &regInfo)) goto failed;
    if (![[HashCopy(source, -1) lowercaseString] isEqualToString:expected]) goto failed;
    {
        NSMutableData *data = [NSMutableData dataWithLength:(NSUInteger)regInfo.st_size];
        size_t offset = 0;
        while (offset < data.length) {
            ssize_t n = read(registry, (char *)data.mutableBytes + offset, data.length - offset);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) goto failed;
            offset += (size_t)n;
        }
        NSData *patched = Patch(data, error);
        if (!patched || patched.length > HiveLimit) goto failed;
        existing = openat(fonts, FontFile.fileSystemRepresentation, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
        if (existing >= 0) {
            if (!Regular(existing, FontLimit, &other) || ![HashCopy(existing, -1) isEqualToString:expected]) goto failed;
        } else if (errno != ENOENT) goto failed;
        BOOL registryChanged = ![data isEqualToData:patched];
        const char *backupName = "system.reg.madeira-cjk-before.reg";
        if (registryChanged) {
            backup = openat(prefix, backupName, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
            if (backup >= 0) {
                if (!Regular(backup, HiveLimit, &other) || other.st_size != (off_t)data.length
                    || !Matches(backup, data)) goto failed;
            } else if (errno != ENOENT) goto failed;
            regTemp = Temporary(prefix, &regName);
            if (regTemp < 0) { FileAccessFail(error, CreateTemporary); goto failed; }
            if (!WriteAll(regTemp, patched.bytes, patched.length)
                || fchmod(regTemp, regInfo.st_mode & 0777) < 0 || fsync(regTemp) < 0) goto failed;
        }
        if (existing < 0) {
            fontTemp = Temporary(fonts, &fontName);
            if (fontTemp < 0) { FileAccessFail(error, CreateTemporary); goto failed; }
            if (![HashCopy(source, fontTemp) isEqualToString:expected]
                || ![HashCopy(fontTemp, -1) isEqualToString:expected]
                || fchmod(fontTemp, 0644) < 0 || fsync(fontTemp) < 0) goto failed;
        }
        // Recheck the original entry immediately before committing offline changes.
        if (fstatat(prefix, "system.reg", &other, AT_SYMLINK_NOFOLLOW) < 0
            || !S_ISREG(other.st_mode) || other.st_ino != regInfo.st_ino
            || other.st_dev != regInfo.st_dev || other.st_size != regInfo.st_size
            || other.st_mtimespec.tv_sec != regInfo.st_mtimespec.tv_sec
            || other.st_mtimespec.tv_nsec != regInfo.st_mtimespec.tv_nsec) goto failed;
        if (registryChanged && backup < 0) {
            backupTemp = Temporary(prefix, &backupTempName);
            if (backupTemp < 0) { FileAccessFail(error, CreateTemporary); goto failed; }
            if (!WriteAll(backupTemp, data.bytes, data.length)
                || fchmod(backupTemp, regInfo.st_mode & 0777) < 0 || fsync(backupTemp) < 0
                || linkat(prefix, backupTempName.fileSystemRepresentation, prefix, backupName, 0) < 0) goto failed;
        }
        if (existing < 0) {
            if (linkat(fonts, fontName.fileSystemRepresentation, fonts, FontFile.fileSystemRepresentation, 0) < 0) goto failed;
            installed = YES;
        }
        if (registryChanged && renameat(prefix, regName.fileSystemRepresentation, prefix, "system.reg") < 0) goto failed;
    }
    ok = YES;
failed:
    if (!ok && installed) unlinkat(fonts, FontFile.fileSystemRepresentation, 0);
    if (fontName && fonts >= 0) unlinkat(fonts, fontName.fileSystemRepresentation, 0);
    if (regName && prefix >= 0) unlinkat(prefix, regName.fileSystemRepresentation, 0);
    if (backupTempName && prefix >= 0) unlinkat(prefix, backupTempName.fileSystemRepresentation, 0);
    int descriptors[] = {regTemp, fontTemp, backupTemp, backup, registry, existing, source, resources, fonts, windows, drive, prefix};
    for (size_t i = 0; i < sizeof(descriptors) / sizeof(descriptors[0]); i++)
        if (descriptors[i] >= 0) close(descriptors[i]);
    if (!ok && (!error || !*error)) Fail(error, @"CJK font preparation failed validation or could not be saved safely.");
    return ok;
}
