/*
 * dar-dtape-explicit-context-6to3.4a focused behavior: exercise the kqchan Mach-port MODIFY path.
 *
 * libkqueue (src/common/kevent.c) routes a re-registration of an EXISTING knote to
 * filt->kn_modify ONLY when the change carries EV_ADD without EV_ENABLE/EV_DISABLE/EV_DELETE
 * (EV_ENABLE goes to kn_enable, EV_DISABLE to kn_disable). So the second kevent below is a bare
 * EV_ADD on the same ident. That reaches evfilt_machport_knote_modify ->
 * dserver_kqchan_msgnum_mach_port_modify on the server side:
 * Kqchan::MachPort::_modify -> dtape_kqchan_mach_port_modify -> filt_machporttouch.
 *
 * The .4a slice removed the impersonate()/impersonate(nullptr) pair around that server call because
 * the touch path reads no current_thread()/current_task(). This fixture fails if the modify path
 * needed the ambient requester after all (the call would not complete with rc 0).
 *
 * Host-built as a guest Mach-O and executed by the prefix; nothing is compiled in the guest.
 */
#include <sys/event.h>
#include <mach/mach.h>
#include <mach/mach_port.h>
#include <stdio.h>

int main(void) {
	int kq = kqueue();
	if (kq < 0) {
		perror("kqueue");
		return 1;
	}

	mach_port_t port = MACH_PORT_NULL;
	kern_return_t kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "mach_port_allocate: %d\n", (int)kr);
		return 1;
	}

	/* Create the knote: a fresh ident with EV_ADD takes the kn_create path. */
	struct kevent ev;
	EV_SET(&ev, port, EVFILT_MACHPORT, EV_ADD, 0, 0, NULL);
	if (kevent(kq, &ev, 1, NULL, 0, NULL) < 0) {
		perror("kevent add");
		return 1;
	}

	/* Modify the existing knote: a bare EV_ADD takes filt->kn_modify. */
	struct kevent mev;
	EV_SET(&mev, port, EVFILT_MACHPORT, EV_ADD, 0, 0, NULL);
	if (kevent(kq, &mev, 1, NULL, 0, NULL) < 0) {
		perror("kevent modify");
		return 1;
	}

	printf("KQCHAN_MODIFY_OK=1\n");
	fflush(stdout);
	return 0;
}
