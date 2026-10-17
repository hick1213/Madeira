/* Actual-source signal/assembly test: no Wine process or device is touched. */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ucontext.h>
#include <unistd.h>

#define WINE_IOS 1
#define FALSE 0
#define TRUE 1
#define STATUS_SUCCESS 0
#define IOS_UNIXLIB_MAX_CODE_STR "256"
#define C_ASSERT(x) _Static_assert(x, #x)
#define DECLSPEC_NORETURN __attribute__((noreturn))
typedef uintptr_t ULONG_PTR;
typedef uint64_t ULONG64;
typedef uint32_t ULONG, DWORD, NTSTATUS;
typedef int BOOL;
typedef __uint128_t NEON128;
struct syscall_frame {
    ULONG64 x[29], fp, lr, sp, pc;
    ULONG cpsr, restore_flags;
    struct syscall_frame *prev_frame;
    void *syscall_cfa;
    ULONG syscall_id, align, fpcr, fpsr;
    NEON128 v[32];
};
struct ntdll_thread_data {
    void *cpu_data[16];
    void *syscall_table;
    struct syscall_frame *syscall_frame;
    int syscall_trace;
    void *kernel_stack;
};
typedef struct { char padding[0x2f0]; struct ntdll_thread_data GdiTebBatch; } TEB;
static pthread_key_t teb_key;
int ios_teb_tls_slot_offset;
static TEB *NtCurrentTeb(void) { return pthread_getspecific(teb_key); }
static struct ntdll_thread_data *ntdll_get_thread_data(void) { return &NtCurrentTeb()->GdiTebBatch; }
static struct syscall_frame *get_syscall_frame(void) { return ntdll_get_thread_data()->syscall_frame; }
static int is_inside_syscall(uintptr_t sp) {
    struct ntdll_thread_data *data = ntdll_get_thread_data();
    return sp >= (uintptr_t)data->kernel_stack && sp <= (uintptr_t)data->syscall_frame;
}
#define SP_sig(c) ((c)->uc_mcontext->__ss.__sp)
#define PC_sig(c) ((c)->uc_mcontext->__ss.__pc)
#define FP_sig(c) ((c)->uc_mcontext->__ss.__fp)
#define LR_sig(c) ((c)->uc_mcontext->__ss.__lr)
#define PSTATE_sig(c) ((c)->uc_mcontext->__ss.__cpsr)
#define REGn_sig(i,c) ((c)->uc_mcontext->__ss.__x[i])
static int ios_thread_ctl_has_teb(void) { return NtCurrentTeb() != NULL; }
static void ios_track_signal(int sig, void *context) { (void)sig; (void)context; }
static void ios_fixup_x18_for_return(void *context) { (void)context; }

uint64_t g_wine_return_x18, g_wine_return_pc, g_wine_return_count, g_wine_return_ring[8];
uint32_t g_wine_return_ring_idx;
int epilogue_kind, epilogue_waiting;
static pthread_mutex_t native_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t slowpath_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_int entered, release_native, signal_seen, aborted, lock_abandoned;
static int slowpath_test, normal_return, use_unixcall, slowpath_queued;
static int saved_x18_mode;
static TEB foreign_teb;
static sigjmp_buf escape;
static const uint64_t result_value = UINT64_C(0xfedcba9876543210);

static DECLSPEC_NORETURN void abort_thread(int status) {
    (void)status;
    if (pthread_mutex_trylock(&native_lock)) atomic_store(&lock_abandoned, 1);
    else pthread_mutex_unlock(&native_lock);
    if (pthread_mutex_trylock(&slowpath_lock)) atomic_store(&lock_abandoned, 1);
    else pthread_mutex_unlock(&slowpath_lock);
    atomic_store(&aborted, 1);
    siglongjmp(escape, 1);
}
static DECLSPEC_NORETURN void user_mode_abort_thread(NTSTATUS status, struct syscall_frame *frame) {
    (void)frame;
    abort_thread(status);
}
static int test_raise(int number) {
    if (number == SIGUSR2 && (slowpath_test == 1 || slowpath_test == 4)) {
        sigset_t mask, pending;
        assert(!pthread_sigmask(SIG_SETMASK, NULL, &mask));
        assert(sigismember(&mask, SIGQUIT));
        assert(!pthread_mutex_lock(&slowpath_lock));
        assert(!raise(SIGQUIT));
        assert(!sigpending(&pending));
        assert(sigismember(&pending, SIGQUIT));
        assert(!atomic_load(&aborted));
        slowpath_queued = 1;
        assert(!pthread_mutex_unlock(&slowpath_lock));
    }
    if (number == SIGUSR2 && slowpath_test == 4) return 0;
    return raise(number);
}
void trace_syscall(void) {}
void trace_sysret(void) {}
uint64_t ios_unixlib_null_call(uint64_t a, uint64_t b, uint64_t c) {
    (void)a; (void)b; (void)c; return 0xc0000002;
}
#define __ASM_NAME(x) "_" x
#define __ASM_LOCAL_LABEL(x) "L" x
#define __ASM_CFI(x) ""
#define __ASM_CFI_CFA_IS_AT2(a,b,c) ""
#define __ASM_GLOBAL_FUNC(name,code) __asm__(".text\n.p2align 2\n.globl _" #name "\n_" #name ":\n" code);
#define raise test_raise
#include "native-quit-source.inc"
#undef raise

static void deliver_quit(int number, siginfo_t *info, void *context) {
    quit_handler(number, info, context);
    atomic_store(&signal_seen, 1);
}

static uint64_t native_call(void *unused) {
    (void)unused;
    struct syscall_frame *frame = get_syscall_frame();
    /* The production frame is guest register state, not native thread identity:
     * Darwin can clear x18 before its save, and CONTEXT_ARM64_X18 can replace it.
     * Keep the actual pthread TEB intact while exercising both valid shapes.
     * Opposite pending state in a foreign TEB also catches a mere NULL guard. */
    if (saved_x18_mode == 1) frame->x[18] = 0;
    if (saved_x18_mode == 2) {
        ios_quit_data(&foreign_teb)->pending = normal_return;
        frame->x[18] = (uintptr_t)&foreign_teb;
    }
    if (slowpath_test) {
        frame->restore_flags = 2;
        frame->x[0] = result_value;
        frame->x[16] = 0; /* Force the real SIGUSR2 context restoration. */
        return result_value;
    }
    if (normal_return || epilogue_kind) return result_value;
    assert(!pthread_mutex_lock(&native_lock));
    atomic_store(&entered, 1);
    while (!atomic_load(&release_native)) {}
    assert(!pthread_mutex_unlock(&native_lock));
    return result_value;
}

/* These wrappers obey the host ABI while the actual dispatchers run with a
 * synthetic Wine TEB and native stack. x19 also detects callee-save damage. */
extern uint64_t invoke_syscall(void);
extern uint64_t invoke_unixcall(void **table, unsigned code, void *arg);
__ASM_GLOBAL_FUNC(invoke_syscall,
    "stp x29, x30, [sp, #-32]!\n"
    "stp x19, x20, [sp, #16]\n"
    "mov x19, #0x123\nmov x8, #0\nmov x9, x30\n"
    "bl ___wine_syscall_dispatcher\n"
    "cmp x19, #0x123\nb.ne 90f\n"
    "ldp x19, x20, [sp, #16]\nldp x29, x30, [sp], #32\nret\n90: brk #0\n")
__ASM_GLOBAL_FUNC(invoke_unixcall,
    "stp x29, x30, [sp, #-32]!\n"
    "stp x19, x20, [sp, #16]\n"
    "mov x19, #0x123\nbl ___wine_unix_call_dispatcher\n"
    "cmp x19, #0x123\nb.ne 90f\n"
    "ldp x19, x20, [sp, #16]\nldp x29, x30, [sp], #32\nret\n90: brk #0\n")

static void *worker(void *unused) {
    (void)unused;
    TEB teb = {0};
    void *stack = mmap(NULL, 1024 * 1024, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    void *table[] = {(void *)native_call};
    unsigned char argument_sizes[] = {0};
    struct { void *funcs; void *counters; uint64_t limit; void *sizes; } service = {table, NULL, 1, argument_sizes};
    assert(stack != MAP_FAILED);
    memset(stack, 0, 1024 * 1024);
    teb.GdiTebBatch.kernel_stack = stack;
    teb.GdiTebBatch.syscall_frame = (struct syscall_frame *)((char *)stack + 1024 * 1024) - 1;
    teb.GdiTebBatch.syscall_table = &service;
    assert(signal_alloc_thread(&teb) == STATUS_SUCCESS);
    assert(!pthread_setspecific(teb_key, &teb));
    uintptr_t tsd;
    __asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(tsd));
    tsd &= ~7ULL;
    for (int i = 0; i < 512; i++) if (((void **)tsd)[i] == &teb) ios_teb_tls_slot_offset = i * 8;
    assert(ios_teb_tls_slot_offset);
    if (slowpath_test == 3) {
        sigset_t mask;
        sigemptyset(&mask); sigaddset(&mask, SIGQUIT); sigaddset(&mask, SIGUSR1);
        assert(!pthread_sigmask(SIG_BLOCK, &mask, NULL));
    }
    if (!sigsetjmp(escape, 1)) {
        uint64_t result = use_unixcall ? invoke_unixcall(table, 0, NULL) : invoke_syscall();
        assert(normal_return && result == result_value);
        sigset_t mask;
        assert(!pthread_sigmask(SIG_SETMASK, NULL, &mask));
        assert(sigismember(&mask, SIGQUIT) == (slowpath_test == 3));
        assert(sigismember(&mask, SIGUSR1) == (slowpath_test == 3));
    }
    assert(!pthread_setspecific(teb_key, NULL));
    assert(!munmap(stack, 1024 * 1024));
    return NULL;
}

static void run_case(int unixcall, int normal, int epilogue, int slowpath) {
    pthread_t thread;
    use_unixcall = unixcall; normal_return = normal; epilogue_kind = epilogue;
    slowpath_test = slowpath; slowpath_queued = 0; epilogue_waiting = 0;
    atomic_store(&entered, 0); atomic_store(&release_native, 0);
    atomic_store(&signal_seen, 0); atomic_store(&aborted, 0); atomic_store(&lock_abandoned, 0);
    assert(!pthread_create(&thread, NULL, worker, NULL));
    if (!normal && !slowpath) {
        while (!(epilogue ? __atomic_load_n(&epilogue_waiting, __ATOMIC_ACQUIRE) : atomic_load(&entered))) usleep(100);
        assert(!pthread_kill(thread, SIGQUIT));
        while (!atomic_load(&signal_seen) && !atomic_load(&aborted)) usleep(100);
        if (!epilogue) {
            if (atomic_load(&aborted)) {
                fputs("FAIL: SIGQUIT aborted a native function while its mutex was held\n", stderr);
                exit(1);
            }
            atomic_store(&release_native, 1);
        }
    }
    assert(!pthread_join(thread, NULL));
    assert(!atomic_load(&lock_abandoned));
    assert(atomic_load(&aborted) == !normal);
    if (slowpath == 1 || slowpath == 4) assert(slowpath_queued);
    assert(!pthread_mutex_trylock(&native_lock));
    assert(!pthread_mutex_unlock(&native_lock));
}

int main(int argc, char **argv) {
    assert(argc == 2);
    saved_x18_mode = atoi(argv[1]);
    assert(saved_x18_mode >= 0 && saved_x18_mode <= 2);
    printf("Testing actual ARM64 dispatchers with saved X18 mode %d\n", saved_x18_mode);
    fflush(stdout);
    alarm(20);
    assert(!pthread_key_create(&teb_key, NULL));
    struct sigaction action = {0};
    action.sa_sigaction = deliver_quit;
    action.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&action.sa_mask);
    sigaddset(&action.sa_mask, SIGQUIT);
    assert(!sigaction(SIGQUIT, &action, NULL));
    action.sa_sigaction = usr2_handler;
    assert(!sigaction(SIGUSR2, &action, NULL));
    run_case(0, 0, 0, 0);
    run_case(1, 0, 0, 0);
    run_case(0, 1, 0, 0);
    run_case(1, 1, 0, 0);
    run_case(0, 0, 1, 0);
    run_case(1, 0, 2, 0);
    run_case(0, 0, 0, 1);
    run_case(1, 0, 0, 1);
    for (int mode = 2; mode <= 4; mode++) {
        run_case(0, mode != 4, 0, mode);
        run_case(1, mode != 4, 0, mode);
    }
    puts("PASS actual ARM64 dispatchers: native locks released before quit; epilogue race, SIGUSR2 slowpath, x0 and x19 preserved");
    return 0;
}
