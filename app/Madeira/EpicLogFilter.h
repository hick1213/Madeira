// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

// Keep whole, bounded lines until redaction, including when Wine truncates a
// credential. An oversized line is discarded rather than persisting a prefix.
typedef struct {
    char secret[257], line[16384], output[8192];
    size_t length, count, used;
    int dropping;
} MadeiraEpicRedactor;

static inline int madeira_epic_redactor_flush(MadeiraEpicRedactor *s, int fd) {
    size_t done = 0;
    while (done < s->used) {
        ssize_t n = write(fd, s->output + done, s->used - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        done += (size_t)n;
    }
    s->used = 0; return 1;
}
static inline int madeira_epic_redactor_emit(MadeiraEpicRedactor *s, int fd, const char *p, size_t n) {
    while (n) {
        size_t room = sizeof(s->output) - s->used;
        if (!room) { if (!madeira_epic_redactor_flush(s, fd)) return 0; room = sizeof(s->output); }
        size_t take = n < room ? n : room;
        memcpy(s->output + s->used, p, take); s->used += take; p += take; n -= take;
    }
    return 1;
}
static inline int madeira_epic_word(unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '-' || c == '_' || c == '.' || c == '~';
}
static inline int madeira_epic_redactor_line(MadeiraEpicRedactor *s, int fd) {
    if (s->dropping) {
        const char notice[] = "[epic] oversized log line omitted\n";
        s->dropping = 0; s->count = 0;
        return madeira_epic_redactor_emit(s, fd, notice, sizeof(notice)-1);
    }
    for (size_t i = 0; i < s->count;) {
        if (s->count-i >= 13 && !strncasecmp(s->line+i, "auth_password", 13)) {
            size_t start = i+13;
            while (start<s->count && (s->line[start]==' ' || s->line[start]=='\t')) start++;
            if (start<s->count && s->line[start]=='=') {
                start++;
                while (start<s->count && (s->line[start]==' ' || s->line[start]=='\t')) start++;
                if (!madeira_epic_redactor_emit(s,fd,s->line+i,start-i) ||
                    !madeira_epic_redactor_emit(s,fd,"[redacted]",10)) return 0;
                size_t end=start;
                if (end<s->count && s->line[end]=='"') {
                    end++;
                    while (end<s->count && s->line[end]!='"' && s->line[end]!='\r' && s->line[end]!='\n') end++;
                    if (end<s->count && s->line[end]=='"') end++;
                } else while (end<s->count && s->line[end]!=' ' && s->line[end]!='\t' && s->line[end]!='\r' && s->line[end]!='\n') end++;
                i=end; continue;
            }
        }
        size_t match=0;
        while (match<s->length && i+match<s->count && s->line[i+match]==s->secret[match]) match++;
        int full = match == s->length;
        int partial = match && match < s->length &&
            (i==0 || !madeira_epic_word((unsigned char)s->line[i-1])) &&
            (i+match==s->count || !madeira_epic_word((unsigned char)s->line[i+match]));
        if (full || partial) {
            if (!madeira_epic_redactor_emit(s,fd,"[redacted]",10)) return 0;
            i+=match;
        } else {
            if (!madeira_epic_redactor_emit(s,fd,s->line+i,1)) return 0;
            i++;
        }
    }
    s->count=0; return 1;
}
static inline int madeira_epic_redactor_init(MadeiraEpicRedactor *s, const char *secret) {
    if (!s || !secret) return 0;
    size_t n=strnlen(secret,257);
    if (n<16 || n>256) return 0;
    memset(s,0,sizeof(*s)); memcpy(s->secret,secret,n); s->length=n; return 1;
}
static inline int madeira_epic_redactor_write(MadeiraEpicRedactor *s, int fd, const void *data, size_t n, int final) {
    const unsigned char *p=data;
    if (!s || !s->length || (!data && n)) return 0;
    for (size_t i=0;i<n;i++) {
        if (!s->dropping) {
            if (s->count==sizeof(s->line)) { s->dropping=1; s->count=0; }
            else s->line[s->count++]=(char)p[i];
        }
        if (p[i]=='\n' && !madeira_epic_redactor_line(s,fd)) return 0;
    }
    if (final && (s->count || s->dropping) && !madeira_epic_redactor_line(s,fd)) return 0;
    return madeira_epic_redactor_flush(s,fd);
}
