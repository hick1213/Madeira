/* Actual-source diagnostic admission plus real Mach -> SIGTRAP child tests. */
#include <assert.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mach/ndr.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ucontext.h>
#include <sys/wait.h>
#include <unistd.h>

typedef uint32_t ULONG;
#define PC_sig(c) ((c)->uc_mcontext->__ss.__pc)
#define REGn_sig(i,c) ((c)->uc_mcontext->__ss.__x[i])

static int read_mode, read_calls, diagnostic_calls, dump_calls, state_writes, last_count;
static uintptr_t detach_pc;
static volatile sig_atomic_t signal_calls;

/* Production instruction reading is retained. Only the syscall boundary is
 * wrapped to cover failure, short reads, and a changed instruction at the owned
 * address without modifying executable memory. Mode 0 uses real Mach reads. */
static kern_return_t observed_read(vm_map_read_t task, mach_vm_address_t address,
                                 mach_vm_size_t size, mach_vm_address_t data,
                                 mach_vm_size_t *outsize) {
    read_calls++;
    if (read_mode == 1) return KERN_INVALID_ADDRESS;
    if (read_mode == 2) { *outsize = 0; return KERN_SUCCESS; }
    kern_return_t result = mach_vm_read_overwrite(task, address, size, data, outsize);
    if (read_mode == 3 && result == KERN_SUCCESS && size == 4) *(uint32_t *)data = 0xd4200020;
    return result;
}

#include "detach-types.inc"
#define mach_vm_read_overwrite observed_read
#include "detach-read.inc"
#undef mach_vm_read_overwrite
#include "detach-stub.inc"

static void ios_dump_jit_pool(const char *why) {
    assert(!strcmp(why, "mach UNHANDLED"));
    dump_calls++;
}
static kern_return_t observed_state_write(thread_t thread, thread_state_flavor_t flavor,
                                         thread_state_t state, mach_msg_type_number_t count) {
    (void)thread; (void)flavor; (void)state; (void)count;
    state_writes++;
    return KERN_SUCCESS;
}
static ios_exc_reply_t process_exception(ios_exc_request_t *req,
                                       const arm_thread_state64_t *input, int handled) {
    arm_thread_state64_t state = *input;
    thread_t thread = req->thread.name;
    mach_msg_type_number_t count = ARM_THREAD_STATE64_COUNT;
    /* Admission, quota statements, and reply construction are extracted intact.
     * The omitted diagnostic body is represented by counters, never file I/O. */
#define thread_set_state observed_state_write
#include "detach-dispatch.inc"
#undef thread_set_state
}

static void wine_protocol_trap(int signal, siginfo_t *siginfo, void *sigcontext) {
    ucontext_t *context = sigcontext;
    (void)signal;
#include "detach-wine-trap.inc"
    _exit(91); /* The unchanged production protocol branch must return first. */
}
static int use_app_fallback;
static void observed_trap(int signal, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    if (PC_sig(uc) != detach_pc) _exit(92);
    if (use_app_fallback) sigtrap_handler(signal, info, context);
    else wine_protocol_trap(signal, info, context);
    if (PC_sig(uc) != detach_pc + 4 || REGn_sig(0, uc) != 0) _exit(93);
    signal_calls++;
}

/* Host ABI wrapper leaves x0 observable across the real void detach stub. */
extern uint64_t invoke_detach(void);
__asm__(".text\n.p2align 2\n.globl _invoke_detach\n_invoke_detach:\n"
        "stp x29, x30, [sp, #-16]!\nmov x0, #0x123\nbl _jit26_detach\n"
        "ldp x29, x30, [sp], #16\nret\n");

static void *exception_server(void *argument) {
    mach_port_t port = (mach_port_t)(uintptr_t)argument;
    union { ios_exc_request_t request; char bytes[1024]; } message;
    assert(mach_msg(&message.request.head, MACH_RCV_MSG, 0, sizeof(message), port,
                    MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL) == KERN_SUCCESS);
    ios_exc_request_t *req = &message.request;
    assert(req->exception == EXC_BREAKPOINT);
    arm_thread_state64_t state;
    mach_msg_type_number_t count = ARM_THREAD_STATE64_COUNT;
    assert(thread_get_state(req->thread.name, ARM_THREAD_STATE64,
                            (thread_state_t)&state, &count) == KERN_SUCCESS);
    assert(arm_thread_state64_get_pc(state) == detach_pc && state.__x[16] == 0);
    int before_diagnostics = diagnostic_calls, before_dumps = dump_calls;
    int before_writes = state_writes, before_count = last_count;
    ios_exc_reply_t reply = process_exception(req, &state, 0);
    assert(reply.ret_code == KERN_FAILURE);
    assert(diagnostic_calls == before_diagnostics && dump_calls == before_dumps);
    assert(state_writes == before_writes && last_count == before_count);
    assert(mach_msg(&reply.head, MACH_SEND_MSG, sizeof(reply), 0,
                    MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL) == KERN_SUCCESS);
    assert(mach_port_deallocate(mach_task_self(), req->thread.name) == KERN_SUCCESS);
    assert(mach_port_deallocate(mach_task_self(), req->task.name) == KERN_SUCCESS);
    return NULL;
}

static void run_live_child(int app_fallback) {
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        alarm(10);
        use_app_fallback = app_fallback;
        struct sigaction action = {0};
        action.sa_sigaction = observed_trap;
        action.sa_flags = SA_SIGINFO;
        sigemptyset(&action.sa_mask);
        assert(!sigaction(SIGTRAP, &action, NULL));
        mach_port_t port;
        assert(mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port) == KERN_SUCCESS);
        assert(mach_port_insert_right(mach_task_self(), port, port, MACH_MSG_TYPE_MAKE_SEND) == KERN_SUCCESS);
        assert(task_set_exception_ports(mach_task_self(), EXC_MASK_BREAKPOINT, port,
                                        EXCEPTION_DEFAULT | MACH_EXCEPTION_CODES,
                                        ARM_THREAD_STATE64) == KERN_SUCCESS);
        pthread_t thread;
        assert(!pthread_create(&thread, NULL, exception_server, (void *)(uintptr_t)port));
        assert(invoke_detach() == 0);
        assert(!pthread_join(thread, NULL));
        assert(signal_calls == 1);
        _exit(0);
    }
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

static void synthetic_case(int exception, uintptr_t pc, uint64_t command,
                           int handled, int expected_diagnostic) {
    ios_exc_request_t req = {0};
    arm_thread_state64_t state = {0};
    req.exception = exception;
    req.head.msgh_id = 2405;
    state.__pc = pc;
    state.__x[16] = command;
    int before_diagnostics = diagnostic_calls, before_writes = state_writes;
    arm_thread_state64_t original = state;
    ios_exc_reply_t reply = process_exception(&req, &state, handled);
    assert(reply.ret_code == (handled ? KERN_SUCCESS : KERN_FAILURE));
    assert(reply.head.msgh_id == 2505);
    assert(diagnostic_calls == before_diagnostics + expected_diagnostic);
    assert(state_writes == before_writes + handled);
    assert(!memcmp(&state, &original, sizeof(state)));
}

int main(void) {
    alarm(30);
    /* Locate the BRK independently in the actual extracted production stub.
     * No test-owned symbol or guessed +4 stands in for production ownership. */
    const uint32_t *code = (const uint32_t *)(uintptr_t)jit26_detach;
    for (int i = 0; i < 16 && !detach_pc; i++)
        if (code[i] == 0xd43e01a0) detach_pc = (uintptr_t)(code + i);
    assert(detach_pc);
    uint32_t opcode = 0;
    assert(ios_fault_read_insn(detach_pc, &opcode) && opcode == 0xd43e01a0);
    for (int i = 0; i < 20; i++)
        synthetic_case(EXC_BREAKPOINT, detach_pc, 0, 0, 0);
    assert(!diagnostic_calls && !dump_calls && !last_count && !state_writes);

    /* Normal handshakes must not consume the first real-fault dump quota. */
    synthetic_case(EXC_BAD_ACCESS, detach_pc, 0, 0, 1);
    assert(last_count == 1 && dump_calls == 1);
    synthetic_case(EXC_BAD_INSTRUCTION, detach_pc, 0, 0, 1);
    synthetic_case(EXC_BREAKPOINT, detach_pc, 1, 0, 1);
    synthetic_case(EXC_BREAKPOINT, detach_pc + 4, 0, 0, 1);
    static const uint32_t foreign_brk = 0xd43e01a0;
    synthetic_case(EXC_BREAKPOINT, (uintptr_t)&foreign_brk, 0, 0, 1);
    synthetic_case(EXC_BREAKPOINT, detach_pc + 1, 0, 0, 1);
    synthetic_case(EXC_BREAKPOINT, 0, 0, 0, 1);
    for (read_mode = 1; read_mode <= 3; read_mode++)
        synthetic_case(EXC_BREAKPOINT, detach_pc, 0, 0, 1);
    read_mode = 0;
    synthetic_case(EXC_BREAKPOINT, detach_pc, 0, 1, 0);
    synthetic_case(EXC_BREAKPOINT, detach_pc, 0, 0, 0);
    assert(diagnostic_calls == 10 && last_count == 10 && dump_calls == 1 && state_writes == 1);
    assert(read_calls >= 24);
    run_live_child(0);
    run_live_child(1);
    puts("PASS 32 diagnostic cases; real Mach KERN_FAILURE -> Wine/app SIGTRAP -> detach return, quota retained");
    return 0;
}
