/* Test the exact production policy functions without adding a sandbox bypass. */
#define main edu_launcher_main
#include "../sandbox.c"
#undef main
#include <sys/socket.h>
#include <signal.h>

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    int landlock = !strcmp(argv[1], "landlock");
    if (!landlock && strcmp(argv[1], "container")) return 2;
    char binary[PATH_MAX];
    if (!realpath("/usr/bin/clangd", binary)) return 3;
    int before = open(argv[3], O_RDONLY);
    if (before < 0) return 4;
    close(before);
    if (landlock) filesystem_policy(argv[2], binary);
    syscall_policy();
    if (landlock) {
        if (open(argv[3], O_RDONLY) >= 0 || errno != EACCES) return 10;
        if (truncate(argv[3], 0) != -1 || errno != EACCES) return 11;
        if (open("/proc/self/environ", O_RDONLY) >= 0 || errno != EACCES) return 12;
    } else {
        /* Container mode deliberately does not promise per-workspace isolation. */
        int readable = open(argv[3], O_RDONLY);
        if (readable < 0) return 19;
        close(readable);
    }
    if (socket(AF_INET, SOCK_STREAM, 0) != -1 || errno != EPERM) return 13;
    if (socket(AF_UNIX, SOCK_STREAM, 0) != -1 || errno != EPERM) return 14;
    if (fork() != -1 || errno != EPERM) return 15;
    struct rlimit value;
    if (syscall(SYS_prlimit64, getppid(), RLIMIT_NOFILE, NULL, &value) != -1 || errno != EPERM) return 16;
    if (syscall(SYS_prlimit64, 0, RLIMIT_NOFILE, NULL, &value)) return 17;
    if (kill(getppid(), 0) != -1 || errno != EPERM) return 18;
    return 0;
}
