#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static int
read_exact(const char* path, const char* expected)
{
	char buf[256] = {0};
	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("open %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 0) {
		printf("read %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (strcmp(buf, expected) != 0) {
		printf("content mismatch for %s: got %s\n", path, buf);
		return 1;
	}
	return 0;
}

static int
write_exact(const char* path, const char* text)
{
	int fd = open(path, O_WRONLY | O_TRUNC);
	if (fd < 0) {
		printf("open write %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	size_t len = strlen(text);
	ssize_t n = write(fd, text, len);
	if (close(fd) != 0) {
		printf("close write %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (n != (ssize_t)len) {
		printf("short write %s: %zd/%zu\n", path, n, len);
		return 1;
	}
	return 0;
}

static int
create_file(const char* path, const char* text)
{
	int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0644);
	if (fd < 0) {
		printf("create %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	size_t len = strlen(text);
	ssize_t n = write(fd, text, len);
	if (close(fd) != 0) {
		printf("close create %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (n != (ssize_t)len) {
		printf("short create write %s: %zd/%zu\n", path, n, len);
		return 1;
	}
	return 0;
}

static int
expect_absent(const char* path)
{
	if (access(path, F_OK) == 0) {
		printf("expected absent but exists: %s\n", path);
		return 1;
	}
	if (errno != ENOENT) {
		printf("access %s failed with unexpected errno: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
bind_unix_socket(const char* path)
{
	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) {
		printf("socket failed: %s\n", strerror(errno));
		return 1;
	}
	struct sockaddr_un addr;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	if (strlen(path) >= sizeof(addr.sun_path)) {
		printf("socket path too long: %s\n", path);
		close(fd);
		return 1;
	}
	strcpy(addr.sun_path, path);
	if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) != 0) {
		printf("bind %s failed: %s\n", path, strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	return 0;
}

int
main(void)
{
	const char* root = "/usr/share/darling/tests/eunion/mutation";

	if (write_exact("/usr/share/darling/tests/eunion/mutation/copy.txt", "UPPER_COPY\n") != 0)
		return 1;
	if (read_exact("/usr/share/darling/tests/eunion/mutation/copy.txt", "UPPER_COPY\n") != 0)
		return 1;

	if (unlink("/usr/share/darling/tests/eunion/mutation/delete.txt") != 0) {
		printf("unlink lower-only delete.txt failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_absent("/usr/share/darling/tests/eunion/mutation/delete.txt") != 0)
		return 1;

	if (create_file("/usr/share/darling/tests/eunion/mutation/create_parent/new.txt", "NEW\n") != 0)
		return 1;
	if (read_exact("/usr/share/darling/tests/eunion/mutation/create_parent/new.txt", "NEW\n") != 0)
		return 1;

	if (rename("/usr/share/darling/tests/eunion/mutation/rename_src",
	    "/usr/share/darling/tests/eunion/mutation/rename_dst") != 0) {
		printf("rename lower dir failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_absent("/usr/share/darling/tests/eunion/mutation/rename_src/child.txt") != 0)
		return 1;
	if (read_exact("/usr/share/darling/tests/eunion/mutation/rename_dst/child.txt", "RENAME_CHILD\n") != 0)
		return 1;

	if (create_file("/usr/share/darling/tests/eunion/mutation/sp_link/new-through-symlink.txt", "SYMLINK_PARENT\n") != 0)
		return 1;
	if (read_exact("/usr/share/darling/tests/eunion/mutation/sp_real/new-through-symlink.txt", "SYMLINK_PARENT\n") != 0)
		return 1;

	/* Materialize the parent, then bind relative to its upper cwd. An
	 * absolute expanded prefix path can exceed AF_UNIX's host path budget. */
	if (create_file("/usr/share/darling/tests/eunion/mutation/bind_parent/anchor",
	    "BIND_UPPER\n") != 0)
		return 1;
	int saved_cwd = open(".", O_RDONLY);
	if (saved_cwd < 0 || chdir("/usr/share/darling/tests/eunion/mutation/bind_parent") != 0) {
		printf("chdir upper bind parent failed: %s\n", strerror(errno));
		return 1;
	}
	int bind_result = bind_unix_socket("sock");
	if (fchdir(saved_cwd) != 0) {
		printf("restore cwd failed: %s\n", strerror(errno));
		close(saved_cwd);
		return 1;
	}
	close(saved_cwd);
	if (bind_result != 0)
		return 1;
	struct stat st;
	if (lstat("/usr/share/darling/tests/eunion/mutation/bind_parent/sock", &st) != 0) {
		printf("lstat bound socket failed: %s\n", strerror(errno));
		return 1;
	}
	if (!S_ISSOCK(st.st_mode)) {
		printf("bound path is not socket: %o\n", st.st_mode);
		return 1;
	}

	(void)root;
	puts("WEST_EUNION_MUTATION_OK");
	return 0;
}
