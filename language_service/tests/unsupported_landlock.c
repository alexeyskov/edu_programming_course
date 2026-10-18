/* Compile-only test seam: emulate a kernel without Landlock. Production does
 * not expose any environment or argument that disables a requested policy. */
#define _GNU_SOURCE
#include <unistd.h>
#include <errno.h>
#include <sys/syscall.h>
#define syscall(number, ...) \
    ((number) == SYS_landlock_create_ruleset \
        ? (errno = ENOSYS, -1L) : syscall(number, __VA_ARGS__))
#include "../sandbox.c"
