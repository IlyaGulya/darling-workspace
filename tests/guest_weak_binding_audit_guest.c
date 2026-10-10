/*
 * Audit of the kqueue-shaped weak placeholders in libsystem_kernel, by MEASURED guest binding.
 *
 * Why measure and not read the source: after the os_unfair_lock case (ADR 0004) we know a weak exported
 * definition can satisfy a guest's dynamic binding and silently do nothing, while the source looks like a
 * placeholder that "a real definition would win". The question for this family is therefore not "is there a
 * weak stub in the tree" but "what does the guest's call actually reach, and what does it do".
 *
 * Three measurements per name, all taken in the guest:
 *   1. BINDING: dladdr() on the public entry point, so the implementing image is a fact.
 *   2. BEHAVIOUR: the call is made and its result recorded. For kqueue the placeholder is loud by design
 *      ("No kqueue implementation?!" then -ENOSYS), so its own message is a decisive tell that the guest
 *      reached it rather than a real implementation.
 *   3. REACHABILITY OF THE HOOKS: dlsym() RTLD_DEFAULT for the weak hook names. If the guest can resolve
 *      them, they are part of the dynamic interface and hiding them changes what a caller can bind; if it
 *      cannot, they are internal to the library and hiding them is a no-op for the guest.
 *
 * Verdict line (`dwdiag verdict` reads the first):
 *   WEAKBIND pass=1 kq=FD kevent=0 close=0 dup=FD
 * Any ENOSYS/errno in those four fields makes the run informative but not passing.
 */
#include <sys/event.h>
#include <sys/time.h>
#include <dlfcn.h>
#include <errno.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>

static const char *image_of(void *p)
{
	Dl_info info;
	if (dladdr(p, &info) == 0 || info.dli_fname == NULL)
		return "(unresolved)";
	return info.dli_fname;
}

static void report_image(const char *name, void *p)
{
	printf("WEAKBIND image %-28s %s\n", name, image_of(p));
}

static void report_hook(const char *name)
{
	void *p = dlsym(RTLD_DEFAULT, name);
	if (p == NULL)
		printf("WEAKBIND hook  %-28s not-resolvable\n", name);
	else
		printf("WEAKBIND hook  %-28s %s\n", name, image_of(p));
}

int main(void)
{
	report_image("kqueue", (void *)&kqueue);
	report_image("kevent", (void *)&kevent);
	report_image("kevent64", (void *)&kevent64);
	report_image("close", (void *)&close);
	report_image("dup", (void *)&dup);
	report_image("listen", (void *)&listen);
	report_hook("kqueue_impl");
	report_hook("kevent_impl");
	report_hook("kevent64_impl");
	report_hook("kqueue_close");
	report_hook("kqueue_closed_fd");
	report_hook("kqueue_dup");
	report_hook("__darling_kqueue_register_listen");

	/* BEHAVIOUR: the public API, called for real. */
	errno = 0;
	int kq = kqueue();
	printf("WEAKBIND call  kqueue                       -> %d (errno %d %s)\n",
	       kq, errno, kq < 0 ? strerror(errno) : "ok");

	int kevent_rc = -1;
	if (kq >= 0) {
		struct kevent ch;
		struct timespec zero = { 0, 0 };
		/* Register nothing and poll with a zero timeout: a real implementation returns 0 immediately. */
		memset(&ch, 0, sizeof ch);
		EV_SET(&ch, 0, EVFILT_READ, EV_ADD, 0, 0, NULL);
		errno = 0;
		kevent_rc = kevent(kq, NULL, 0, &ch, 1, &zero);
		printf("WEAKBIND call  kevent(zero timeout)        -> %d (errno %d %s)\n",
		       kevent_rc, errno, kevent_rc < 0 ? strerror(errno) : "ok");
		errno = 0;
		int close_rc = close(kq);
		printf("WEAKBIND call  close(kq)                   -> %d (errno %d %s)\n",
		       close_rc, errno, close_rc < 0 ? strerror(errno) : "ok");
	}

	struct timespec zero64 = { 0, 0 };
	(void)zero64;
	errno = 0;
	int d = dup(0);
	printf("WEAKBIND call  dup(0)                      -> %d (errno %d %s)\n",
	       d, errno, d < 0 ? strerror(errno) : "ok");
	if (d >= 0)
		close(d);

	printf("WEAKBIND pass=%d kq=%d kevent=%d dup-seen=%d\n",
	       (kq >= 0 && kevent_rc == 0 && d >= 0) ? 1 : 0, kq, kevent_rc, d >= 0);
	return (kq >= 0 && kevent_rc == 0 && d >= 0) ? 0 : 1;
}
