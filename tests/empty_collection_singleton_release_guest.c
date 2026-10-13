/*
 * empty_collection_singleton_release_guest.c -- permanent guest oracle that the
 * static-storage empty-collection singletons survive being released.
 *
 * WHY THIS EXISTS. The Xcode ladder (scripts/xcodebuild-workload-run.sh, both
 * workloads) dies inside Xcode3Core's -[Xcode3CommandLineBuildTool
 * _resolvePackageDependenciesAndExit] block, which calls _objc_release (libobjc)
 * on an object that is CoreFoundation's empty-array singleton in static storage:
 *
 *   #3 free + 0x25d                     libsystem_malloc.dylib
 *   #4 Xcode3Core + 0x3af97             (return address after `callq *%r15`, r15 = _objc_release)
 *   xcodebuild(...) malloc: *** error for object 0x7b65ca1d84b8: pointer being freed was not allocated
 *
 * 0x7b65ca1d84b8 is exactly `___NSArray0__struct` (CoreFoundation's __DATA, the
 * object the __NSArray0__ pointer variable points at), and the object's isa
 * decodes to _OBJC_CLASS_$_NSConstantArray. NSConstantArray implemented no
 * retain/release/autorelease/retainCount of its own, so objc4 marked it "default
 * RR" and _objc_release decremented the isa's retain count, reached zero and
 * freed static storage the allocator never handed out. On macOS the empty
 * collection is an immortal constant collection, so the same release is a no-op.
 *
 * WHAT THIS FIXTURE MEASURES, one subject per child process (a child that dies
 * must not take the verdict with it):
 *
 *   1. array-singleton-objc_release     _objc_release(__NSArray0__) x3
 *   2. array-singleton-release-message  [__NSArray0__ release] x3
 *   3. dictionary-singleton-objc_release _objc_release(__NSDictionary0__) x3
 *   4. constant-array-instance          an NSConstantArray in THIS image's static
 *                                       storage (the shape a compiler-emitted
 *                                       literal has) released x3
 *   5. control-double-free              free(p) twice: the allocator's own
 *                                       report must still fire, so the fix
 *                                       cannot be "stop reporting invalid frees"
 *
 * A subject passes only if the child SURVIVES and the object still answers
 * -count with 0 afterwards; a child that dies is reported with its signal. The
 * control passes only if its child DIES (SIGILL from malloc's abort).
 *
 * The runtime and CoreFoundation are reached through dlopen/dlsym on purpose:
 * this file compiles with the guest CommandLineTools SDK alone, with no ObjC or
 * CF headers and no -framework link flags.
 *
 * Raw measurement lines are printed unconditionally (RELPROBE-*) so a RED arm
 * can quote the failing subject from the run's own output; the verdict line is
 * machine-readable and read by the harness's ok-marker.
 *
 * MEASURED on the unfixed runtime (prefix-verify14, 2026-10-13), one line per
 * subject, and this is the RED arm the metadata declares:
 *
 *   array-singleton-objc_release     DIED signal=4  malloc: *** error for object
 *                                    <singleton>: pointer being freed was not allocated
 *   array-singleton-release-message  DIED signal=4  (same report)
 *   dictionary-singleton-objc_release DIED signal=11 (SIGSEGV, no malloc report:
 *                                    __NSCFDictionary's -release routes to
 *                                    CFRelease, which re-dispatches -release for
 *                                    an object that is not a CF object)
 *   constant-array-instance          DIED signal=4  (same report, for an
 *                                    NSConstantArray in this image's static data)
 *   control-double-free              DIED signal=4  (required in BOTH arms)
 */

#include <dlfcn.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"

#define SUBJECT_ARRAY_OBJC  "array-singleton-objc_release"
#define SUBJECT_ARRAY_MSG   "array-singleton-release-message"
#define SUBJECT_DICT_OBJC   "dictionary-singleton-objc_release"
#define SUBJECT_LITERAL     "constant-array-instance"
#define SUBJECT_CONTROL     "control-double-free"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClass)(void *);
static const char *(*className)(void *);
static void (*objcRelease)(void *);
static void *(*constructInstance)(void *, void *);

static int failures;

/* Storage for the compiler-literal-shaped subject: an NSConstantArray instance
 * in this image's own static data, which is where clang puts `@[@"x"]`. */
static unsigned char literal_storage[256] __attribute__((aligned(16)));

static void *lookup(void *handle, const char *name)
{
	void *found = dlsym(handle, name);
	if (!found) {
		/* dlsym's own underscore handling is not the contract this fixture
		 * rests on: try the Mach-O spelling explicitly as well. */
		char underscored[128];
		snprintf(underscored, sizeof underscored, "_%s", name);
		found = dlsym(handle, underscored);
	}
	if (!found && name[0] == '_') {
		found = dlsym(handle, name + 1);
	}
	if (!found) {
		printf("RELPROBE-FAIL symbol=%s error=%s\n", name, dlerror());
		exit(1);
	}
	return found;
}

static void resolve_runtime(void)
{
	void *objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc) {
		printf("RELPROBE-FAIL dlopen %s: %s\n", OBJC_LIBRARY, dlerror());
		exit(1);
	}
	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClass = (void *(*)(void *))lookup(objc, "object_getClass");
	className = (const char *(*)(void *))lookup(objc, "class_getName");
	constructInstance = (void *(*)(void *, void *))lookup(objc, "objc_constructInstance");
	/* The exact entry point the Xcode block calls (libobjc + 0x6a00). */
	objcRelease = (void (*)(void *))lookup(objc, "_objc_release");
}

static void *corefoundation_handle(void)
{
	void *cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!cf) {
		printf("RELPROBE-FAIL dlopen %s: %s\n", CF_LIBRARY, dlerror());
		exit(1);
	}
	return cf;
}

/* The object a client gets by referencing __NSArray0__ / __NSDictionary0__: the
 * symbol is the pointer variable, its value is the object in CF's static data. */
static void *singleton(void *cf, const char *symbol)
{
	void **variable = (void **)lookup(cf, symbol);
	return *variable;
}

static long count_of(void *object)
{
	return (long)msgSend(object, selRegister("count"));
}

static long retain_count_of(void *object)
{
	return (long)msgSend(object, selRegister("retainCount"));
}

enum release_mode {
	RELEASE_OBJC = 0,
	RELEASE_MESSAGE = 1,
};

struct subject {
	const char *name;
	void *object;
	int mode;			/* enum release_mode */
	const char *expect_class;	/* evidence, not a gate */
};

/* Runs in the child. Writes its status line to the pipe and exits 0 only when
 * the object is still usable. */
static void child_run(const struct subject *s, int write_fd)
{
	char status[256];
	int length;

	printf("RELPROBE-START subject=%s object=%p class=%s isa=%#llx retainCount=%ld\n", s->name, s->object,
	    className(getClass(s->object)), (unsigned long long)*(unsigned long long *)s->object,
	    retain_count_of(s->object));
	fflush(stdout);

	for (int i = 0; i < 3; i++) {
		if (s->mode == RELEASE_OBJC) {
			objcRelease(s->object);
		} else {
			msgSend(s->object, selRegister("release"));
		}
	}

	long count = count_of(s->object);
	long retain_count = retain_count_of(s->object);
	const char *cls = className(getClass(s->object));
	printf("RELPROBE-SURVIVED subject=%s class=%s count=%ld retainCount=%ld\n", s->name, cls, count, retain_count);
	fflush(stdout);

	length = snprintf(status, sizeof status, "survived count=%ld retainCount=%ld class=%s", count, retain_count, cls);
	if (write(write_fd, status, (size_t)length) != length) {
		_exit(2);
	}
	close(write_fd);
	_exit(0);
}

/* A child that must die: the allocator's invalid-free report has to keep firing. */
static void child_double_free(int write_fd)
{
	void *p = malloc(64);
	free(p);
	free(p);
	printf("RELPROBE-CONTROL-SURVIVED subject=%s\n", SUBJECT_CONTROL);
	fflush(stdout);
	(void)write(write_fd, "survived", 8);
	_exit(0);
}

/* Forks one child, collects its death/status, prints the verdict line and counts
 * failures. expect_death inverts the requirement (the control). */
static void run_subject(const struct subject *s, int expect_death)
{
	int pipefd[2];
	pid_t pid;
	int status = 0;
	char buffer[256] = {0};
	ssize_t got = 0;

	if (pipe(pipefd) != 0) {
		printf("RELPROBE-FAIL subject=%s pipe failed\n", s->name);
		failures++;
		return;
	}
	pid = fork();
	if (pid < 0) {
		printf("RELPROBE-FAIL subject=%s fork failed\n", s->name);
		failures++;
		close(pipefd[0]);
		close(pipefd[1]);
		return;
	}
	if (pid == 0) {
		close(pipefd[0]);
		if (expect_death) {
			child_double_free(pipefd[1]);
		}
		child_run(s, pipefd[1]);
		_exit(3);
	}
	close(pipefd[1]);
	got = read(pipefd[0], buffer, sizeof buffer - 1);
	close(pipefd[0]);
	if (got < 0) {
		got = 0;
	}
	buffer[got] = '\0';
	while (waitpid(pid, &status, 0) < 0) {
		/* retry on EINTR */
	}

	if (WIFSIGNALED(status)) {
		int sig = WTERMSIG(status);
		printf("RELPROBE-DIED subject=%s signal=%d%s\n", s->name, sig,
		    sig == SIGILL ? " (SIGILL: the allocator's abort)" : "");
		if (expect_death) {
			printf("RELPROBE-CONTROL-OK subject=%s died as required\n", s->name);
		} else {
			failures++;
		}
		return;
	}

	if (expect_death) {
		printf("RELPROBE-CONTROL-SURVIVED subject=%s status=%s\n", s->name, buffer);
		failures++;
		return;
	}

	if (WEXITSTATUS(status) != 0) {
		printf("RELPROBE-BAD subject=%s exit=%d status=%s\n", s->name, WEXITSTATUS(status), buffer);
		failures++;
		return;
	}

	long count = -1;
	if (sscanf(buffer, "survived count=%ld", &count) != 1 || count != 0) {
		printf("RELPROBE-BAD subject=%s status=%s\n", s->name, buffer);
		failures++;
		return;
	}

	printf("RELPROBE-OK subject=%s status=%s expect_class=%s\n", s->name, buffer, s->expect_class);
}

int main(void)
{
	void *cf;
	struct subject subjects[4];

	/* Line buffered: every child forks from this process, and a child that
	 * flushes an inherited block buffer would re-emit the parent's lines. */
	setvbuf(stdout, NULL, _IOLBF, 0);

	resolve_runtime();
	cf = corefoundation_handle();

	void *empty_array = singleton(cf, "__NSArray0__");
	void *empty_dictionary = singleton(cf, "__NSDictionary0__");
	/* dlsym on a class symbol yields the class object's address. */
	void *constant_array_class = lookup(cf, "_OBJC_CLASS_$_NSConstantArray");

	memset(literal_storage, 0, sizeof literal_storage);
	void *literal_array = constructInstance(constant_array_class, literal_storage);

	printf("RELPROBE-ENV array=%p dictionary=%p literal=%p constant_array_class=%p\n",
	    empty_array, empty_dictionary, literal_array, constant_array_class);
	fflush(stdout);

	subjects[0] = (struct subject){ SUBJECT_ARRAY_OBJC, empty_array, RELEASE_OBJC, "NSConstantArray" };
	subjects[1] = (struct subject){ SUBJECT_ARRAY_MSG, empty_array, RELEASE_MESSAGE, "NSConstantArray" };
	subjects[2] = (struct subject){ SUBJECT_DICT_OBJC, empty_dictionary, RELEASE_OBJC, "NSConstantDictionary" };
	subjects[3] = (struct subject){ SUBJECT_LITERAL, literal_array, RELEASE_OBJC, "NSConstantArray" };

	for (unsigned i = 0; i < sizeof subjects / sizeof subjects[0]; i++) {
		run_subject(&subjects[i], 0);
	}

	struct subject control = { SUBJECT_CONTROL, NULL, RELEASE_OBJC, NULL };
	run_subject(&control, 1);

	if (failures != 0) {
		printf("EMPTY_SINGLETON_RELEASE_FAIL failures=%d\n", failures);
		fflush(stdout);
		return 1;
	}
	printf("EMPTY_SINGLETON_RELEASE_OK\n");
	fflush(stdout);
	return 0;
}
