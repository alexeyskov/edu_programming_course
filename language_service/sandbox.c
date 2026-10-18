/* Unprivileged Linux launcher for one clangd workspace.
 * No student-selected executable, flags, compiler plugin, config or environment.
 * Default container isolation matches the existing runner's filesystem boundary.
 * Optional, fail-closed Landlock ABI >=3 protects file reads/writes/truncation.
 * In both modes seccomp prevents
 * network access, child processes, cross-process inspection and namespace APIs.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif

static _Noreturn void fail(const char *reason) {
    /* No paths or environment values in messages. */
    fprintf(stderr, "clangd sandbox: %s\n", reason);
    _exit(125);
}

static void allow_path(int rules, const char *path, uint64_t access, int optional) {
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0) { if (optional && errno == ENOENT) return; fail("cannot open allowed path"); }
    struct landlock_path_beneath_attr rule = {.allowed_access = access, .parent_fd = fd};
    if (syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH, &rule, 0)) fail("cannot add filesystem rule");
    close(fd);
}

static void filesystem_policy(const char *workspace, const char *binary) {
    int abi = syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 3) fail("Landlock ABI 3 or newer is required");
    uint64_t handled = (1ULL << 15) - 1;
    struct landlock_ruleset_attr attr = {.handled_access_fs = handled};
    int rules = syscall(SYS_landlock_create_ruleset, &attr, sizeof(attr), 0);
    if (rules < 0) fail("cannot create filesystem policy");
    uint64_t read = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR;
    /* Do not expose /usr/local (service code), /etc (except loader cache),
       /proc, /tmp, the workspace parent, secret files or other sessions. */
    allow_path(rules, "/usr/include", read, 0);
    allow_path(rules, "/usr/lib", read, 0);
    allow_path(rules, "/lib", read, 0);
    allow_path(rules, "/lib64", read, 1);
    allow_path(rules, "/etc/ld.so.cache", LANDLOCK_ACCESS_FS_READ_FILE, 1);
    allow_path(rules, "/dev/null", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE, 0);
    allow_path(rules, "/dev/urandom", LANDLOCK_ACCESS_FS_READ_FILE, 1);
    allow_path(rules, binary, LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE, 0);
    /* execve of a dynamic ELF also executes its trusted PT_INTERP loader. */
#if defined(__x86_64__)
    allow_path(rules, "/lib64/ld-linux-x86-64.so.2", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE, 0);
#elif defined(__aarch64__)
    allow_path(rules, "/lib/ld-linux-aarch64.so.1", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE, 0);
#endif
    uint64_t work = read | LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_MAKE_DIR |
        LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_TRUNCATE;
    allow_path(rules, workspace, work, 0);
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) fail("cannot restrict privileges");
    if (syscall(SYS_landlock_restrict_self, rules, 0)) fail("cannot enforce filesystem policy");
    close(rules);
}

#define DENY(nr) BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 1), BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)

static void syscall_policy(void) {
#if defined(__x86_64__)
    const uint32_t arch = AUDIT_ARCH_X86_64;
#elif defined(__aarch64__)
    const uint32_t arch = AUDIT_ARCH_AARCH64;
#else
#error "Unsupported sandbox architecture"
#endif
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, arch, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
        DENY(SYS_socket), DENY(SYS_socketpair), DENY(SYS_connect), DENY(SYS_bind),
        DENY(SYS_ptrace), DENY(SYS_process_vm_readv), DENY(SYS_process_vm_writev),
        DENY(SYS_kcmp), DENY(SYS_bpf), DENY(SYS_perf_event_open), DENY(SYS_userfaultfd),
        DENY(SYS_mount), DENY(SYS_umount2), DENY(SYS_pivot_root), DENY(SYS_chroot),
        DENY(SYS_setns), DENY(SYS_unshare), DENY(SYS_open_by_handle_at),
        DENY(SYS_execveat), DENY(SYS_io_uring_setup),
        DENY(SYS_sched_setaffinity), DENY(SYS_sched_setscheduler), DENY(SYS_sched_setparam), DENY(SYS_setpriority),
        DENY(SYS_kill), DENY(SYS_tkill), DENY(SYS_rt_sigqueueinfo), DENY(SYS_rt_tgsigqueueinfo),
        DENY(SYS_pidfd_open), DENY(SYS_pidfd_getfd), DENY(SYS_pidfd_send_signal),
        /* Never change/read another same-UID process's limits. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_prlimit64, 0, 5),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)getpid(), 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
#ifdef SYS_fork
        DENY(SYS_fork), DENY(SYS_vfork),
#endif
        /* glibc retries clone when clone3 is unavailable. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        /* clangd needs threads, not additional processes or namespaces. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 6),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_THREAD, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWCGROUP, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        /* Thread-directed signals may only target this process. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_tgkill, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)getpid(), 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {.len = sizeof(filter) / sizeof(filter[0]), .filter = filter};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program)) fail("cannot enforce syscall policy");
}

static void limit(int resource, rlim_t maximum) {
    struct rlimit value = {.rlim_cur = maximum, .rlim_max = maximum};
    if (setrlimit(resource, &value)) fail("cannot enforce resource limit");
}

int main(int argc, char **argv) {
    if (argc < 4 || strcmp(argv[1], "--isolation")) fail("invalid launcher arguments");
    int landlock;
    if (!strcmp(argv[2], "container")) landlock = 0;
    else if (!strcmp(argv[2], "landlock")) landlock = 1;
    else fail("invalid isolation mode");
    if (argc == 4 && !strcmp(argv[3], "--probe")) {
        char current[PATH_MAX], binary[PATH_MAX];
        if (!getcwd(current, sizeof(current)) || !realpath("/usr/bin/clangd", binary)) fail("probe executable unavailable");
        if (landlock) filesystem_policy(current, binary);
        syscall_policy();
        char *arguments[] = {binary, "--version", NULL};
        execv(binary, arguments);
        fail("cannot execute clangd during readiness probe");
    }
    if (argc < 6) fail("invalid launcher arguments");
    char workspace[PATH_MAX], binary[PATH_MAX];
    if (!realpath(argv[3], workspace) || !realpath(argv[5], binary)) fail("invalid launcher paths");
    if (strncmp(binary, "/usr/lib/llvm-", 14) && strncmp(binary, "/usr/bin/clangd", 15)) fail("executable must be packaged clangd");
    char *end;
    long memory = strtol(argv[4], &end, 10);
    if (*end || memory < 256 || memory > 4096) fail("invalid resource limit");
    limit(RLIMIT_AS, (rlim_t)memory * 1024 * 1024);
    limit(RLIMIT_FSIZE, 32 * 1024 * 1024);
    limit(RLIMIT_NOFILE, 128);
    limit(RLIMIT_CORE, 0);
    if (chdir(workspace)) fail("cannot select workspace");
    if (landlock) filesystem_policy(workspace, binary);
    syscall_policy();
    execv(binary, &argv[5]);
    fail("cannot start clangd");
}
