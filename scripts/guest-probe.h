/*
 * guest-probe.h -- diagnostic probes for Darling guest code (dyld, launchd, mldr, libsystem_kernel).
 *
 * WHY THIS EXISTS. A survey of one investigation cycle found twelve probes that produced either silence or a
 * WRONG RESULT, and the causes were always the same three:
 *
 *   1. the probe needed a runtime that was not up yet (libc: write(), strlen(), getenv() in dyld's bootstrap);
 *   2. the probe was not in the artifact under test (compiled into a file nothing linked);
 *   3. the probe MODIFIED the state it was measuring (a probe in dyld's entry stub loaded 1 into %rax and then
 *      `jmp *%rax` jumped to address 1; a second one clobbered %rdi, which held argc, so `main` saw argc=2).
 *
 * Each rule below exists because an instance of it cost real time.
 *
 * RULES
 *   - DARLING_PROBE writes with a RAW syscall: no libc, fixed length, no allocation. It works from the first
 *     instruction of a process, before any runtime exists.
 *   - The tag is a SINGLE string literal, so the probe is verifiable with `strings <artifact> | grep <tag>`.
 *     An earlier design built the tag from fragments and made verification impossible.
 *   - DARLING_PROBE_SAVED keeps the caller's ABI registers intact across the syscall. `syscall` clobbers %rcx
 *     and %r11 by definition; this wrapper also preserves %rax, %rdi, %rsi and %rdx, which are the argument
 *     registers a caller may be about to consume -- that is the exact class of bug that produced `jmp 1`.
 *   - Probes live behind a single compile-time switch or an environment check made with a RAW read of
 *     /proc/self/environ, never with getenv(); they must be removable in one edit for an acceptance run.
 */

#ifndef DARLING_GUEST_PROBE_H

/*
 * THE SYSCALL NUMBER IS LINUX-NUMBERED, AND GETTING IT WRONG IS SILENT.
 *
 * The raw `syscall` instruction in a Darling guest takes a LINUX syscall number in this context: write is 1,
 * not 4. A probe written with rax=4 therefore EXECUTES and does something else entirely (Linux `stat`), printing
 * nothing -- which is indistinguishable from "the code under test was never reached". This was measured: probes
 * placed on a hot path in libsystem_kernel never appeared in any run log while a probe in launchd, written with
 * rax=1, appeared every time, and both were raw `syscall` writes to fd 2.
 *
 * So: write is __NR_write == 1 here. If a probe is silent, verify the NUMBER before concluding anything about the
 * code path. The tag's presence in the artifact (see darling-describe-artifact.sh) rules out the other cause.
 */

#define DARLING_GUEST_PROBE_H

#if defined(__x86_64__)

/* write(2, msg, len) as a raw syscall; clobbers only what `syscall` clobbers. */
static __inline__ void __darling_probe_write(const char* msg, unsigned long len)
{
	register long rax __asm__("rax") = 1;   /* SYS_write */
	register long rdi __asm__("rdi") = 2;   /* fd 2 = stderr */
	register long rsi __asm__("rsi") = (long)(unsigned long)msg;
	register long rdx __asm__("rdx") = (long)len;
	__asm__ volatile("syscall" : "+r"(rax) : "r"(rdi), "r"(rsi), "r"(rdx) : "rcx", "r11", "memory");
}

/* A probe that PRESERVES the registers a surrounding contract may depend on. Use this anywhere the code after
 * the probe consumes %rax/%rdi/%rsi/%rdx (an entry stub, a call sequence, a return path). */
#define DARLING_PROBE_SAVED(tag)                                            \
	do {                                                                    \
		register long __p_rax __asm__("rax");                               \
		register long __p_rdi __asm__("rdi");                               \
		register long __p_rsi __asm__("rsi");                               \
		register long __p_rdx __asm__("rdx");                               \
		__asm__ volatile("" : "=r"(__p_rax), "=r"(__p_rdi),                 \
		                 "=r"(__p_rsi), "=r"(__p_rdx));                     \
		__asm__ volatile("pushq %rax; pushq %rdi; pushq %rsi; pushq %rdx;   \
		                  pushq %rcx; pushq %r11");                         \
		__darling_probe_write("[" tag "]\n", sizeof("[" tag "]\n") - 1);    \
		__asm__ volatile("popq %r11; popq %rcx; popq %rdx; popq %rsi;       \
		                  popq %rdi; popq %rax");                           \
		__asm__ volatile("" :: "r"(__p_rax), "r"(__p_rdi),                  \
		                 "r"(__p_rsi), "r"(__p_rdx));                       \
	} while (0)

/* The plain form: call it where nothing after it depends on the argument registers. */
#define DARLING_PROBE(tag) __darling_probe_write("[" tag "]\n", sizeof("[" tag "]\n") - 1)

/* Verification helper: a probe is only trustworthy if it is IN the artifact. Because the tag above is a single
 * literal, this is exact:
 *     strings <artifact> | grep -c '\[my-tag\]'
 * Run it on the BUILT file and on every DEPLOYED copy; a probe that is missing from the deployed copy means the
 * measurement will be silent and the conclusion drawn from it will be wrong. */

#endif /* __x86_64__ */

#endif /* DARLING_GUEST_PROBE_H */
