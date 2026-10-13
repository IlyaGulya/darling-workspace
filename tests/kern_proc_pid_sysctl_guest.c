/*
 * kern_proc_pid_sysctl_guest.c -- permanent guest oracle for the
 * sysctl(CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()) query.
 *
 * WHY THIS EXISTS. DVTFoundation's CurrentProcessIsBeingDebugged() does exactly
 * this call and, when sysctl() returns non-zero, fires its soft assertion
 * ("Call to sysctl failed", DVTToolsInfo.m:97), whose handler re-enters
 * -[DVTToolsInfo runtimeInternalState] through a dispatch_once gate the caller
 * already owns and reaches libdispatch's deliberate ud2 ("trying to lock
 * recursively"). On macOS the call always succeeds for the caller's own pid, so
 * the macOS-shaped contract this test pins is: rc == 0, the returned
 * kp_proc.p_pid equals the pid asked about, and p_flag does not claim the
 * process is being traced. It also pins the negative case, which on macOS
 * returns no record at all (rc 0 with oldlen 0: kern_sysctl.c
 * sysctl_prochandle copies nothing when no proc matches).
 *
 * The raw measurement lines are printed unconditionally (KPPROBE ...) so a RED
 * arm can quote rc/errno/p_pid/p_flag from the run's own output, and the
 * verdict line is machine-readable and read by the harness's ok-marker.
 */

#include <sys/types.h>
#include <sys/sysctl.h>
#include <sys/stat.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <dirent.h>

#ifndef P_TRACED
#define P_TRACED 0x00000800
#endif

/* An explicitly bogus pid: above the maximum on both Linux and Darwin. */
#define BOGUS_PID 0x7fffff00

static int failures;

static void fail(const char *msg)
{
	printf("KPPROBE-FAIL %s\n", msg);
	failures++;
}

/*
 * The container view of us: is the pid getpid() returns a numeric entry of the
 * /proc whose text _sysctl_proc() parses, and what pid does that entry call
 * itself? This is the measurement that separates "the caller's pid and the
 * container pid differ" from any other cause of a failed query.
 */
static void probe_proc_namespace(pid_t self)
{
	char path[64];
	struct stat st;
	DIR *d;
	struct dirent *e;
	int numeric = 0, has_self = 0, highest = 0;

	snprintf(path, sizeof(path), "/proc/%d/stat", (int) self);
	errno = 0;
	printf("KPPROBE stat_self rc=%d errno=%d\n", stat(path, &st), errno);

	FILE *f = fopen(path, "r");
	if (f != NULL) {
		char line[512];
		if (fgets(line, sizeof(line), f) != NULL) {
			char *nl = strchr(line, '\n');
			if (nl != NULL) {
				*nl = '\0';
			}
			printf("KPPROBE stat_self_line=%s\n", line);
		}
		fclose(f);
	} else {
		printf("KPPROBE stat_self_open_failed errno=%d\n", errno);
	}

	d = opendir("/proc");
	if (d != NULL) {
		while ((e = readdir(d)) != NULL) {
			int v;
			if (e->d_name[0] < '1' || e->d_name[0] > '9') {
				continue;
			}
			v = atoi(e->d_name);
			if (v <= 0) {
				continue;
			}
			numeric++;
			if (v > highest) {
				highest = v;
			}
			if (v == (int) self) {
				has_self = 1;
			}
		}
		closedir(d);
	} else {
		printf("KPPROBE opendir_proc_failed errno=%d\n", errno);
	}
	printf("KPPROBE proc_census numeric=%d has_self=%d highest=%d\n",
	    numeric, has_self, highest);
}

int main(void)
{
	pid_t self = getpid();
	struct kinfo_proc kp;
	size_t len;
	int rc, err;

	printf("KPPROBE getpid=%d getppid=%d\n", (int) self, (int) getppid());

	probe_proc_namespace(self);

	/* --- the load-bearing case: our own pid, exactly as DVTFoundation asks --- */
	{
		int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, (int) self };

		memset(&kp, 0, sizeof(kp));
		len = sizeof(kp);
		errno = 0;
		rc = sysctl(mib, 4, &kp, &len, NULL, 0);
		err = errno;
		printf("KPPROBE self rc=%d errno=%d len=%zu p_pid=%d p_flag=0x%x p_comm=%s\n",
		    rc, err, len, kp.kp_proc.p_pid, kp.kp_proc.p_flag, kp.kp_proc.p_comm);

		if (rc != 0) {
			fail("self KERN_PROC_PID returned non-zero (macOS returns 0)");
		} else {
			if (kp.kp_proc.p_pid != (int) self) {
				fail("self KERN_PROC_PID reported a different pid");
			}
			if (len != sizeof(kp)) {
				fail("self KERN_PROC_PID returned no record (len != sizeof kinfo_proc)");
			}
			if ((kp.kp_proc.p_flag & P_TRACED) != 0) {
				fail("self KERN_PROC_PID claims the process is being traced");
			}
		}

		/* Size pre-flight, the other shape a caller may use (oldp == NULL). */
		len = 0;
		errno = 0;
		rc = sysctl(mib, 4, NULL, &len, NULL, 0);
		printf("KPPROBE self_sizequery rc=%d errno=%d len=%zu\n", rc, errno, len);
		if (rc != 0) {
			fail("self KERN_PROC_PID size query returned non-zero");
		}
		if (len == 0) {
			fail("self KERN_PROC_PID size query found no record");
		}
	}

	/* --- negative case: a pid that cannot exist must yield no record --- */
	{
		int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, BOGUS_PID };

		memset(&kp, 0, sizeof(kp));
		len = sizeof(kp);
		errno = 0;
		rc = sysctl(mib, 4, &kp, &len, NULL, 0);
		err = errno;
		printf("KPPROBE bogus rc=%d errno=%d len=%zu p_pid=%d p_flag=0x%x\n",
		    rc, err, len, kp.kp_proc.p_pid, kp.kp_proc.p_flag);

		/* macOS: rc 0, nothing copied (oldlen 0). Any successful non-empty
		 * answer for a pid that cannot exist is a wrong-answer defect. */
		if (rc == 0 && len != 0) {
			fail("bogus pid was answered with a process record");
		}
	}

	/*
	 * The general handler, for the record only: KERN_PROC_ALL is a THREE-element MIB
	 * ({CTL_KERN, KERN_PROC, KERN_PROC_ALL}) and handle_proc() rejects any nlen < 4, so this
	 * query returns ENOENT for a reason that predates this fix and is not part of it. It is
	 * printed, not asserted, so the fixture stays about the KERN_PROC_PID contract.
	 */
	{
		int mib[3] = { CTL_KERN, KERN_PROC, KERN_PROC_ALL };
		size_t all_len = 0;

		errno = 0;
		rc = sysctl(mib, 3, NULL, &all_len, NULL, 0);
		printf("KPPROBE all_sizequery rc=%d errno=%d len=%zu\n", rc, errno, all_len);
	}

	printf("KPPROBE failures=%d\n", failures);
	if (failures == 0) {
		printf("KERN_PROC_PID_SYSCTL_OK\n");
		return 0;
	}
	printf("KERN_PROC_PID_SYSCTL_FAIL\n");
	return 1;
}
