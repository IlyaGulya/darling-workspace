#include <sys/attr.h>
#include <sys/fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#ifndef FSOPT_NOFOLLOW
#define FSOPT_NOFOLLOW 1
#endif
#ifndef FSOPT_REPORT_FULLSIZE
#define FSOPT_REPORT_FULLSIZE 4
#endif
#ifndef ATTR_CMN_CRTIME
#define ATTR_CMN_CRTIME 0x00000200
#endif
#ifndef VLNK
#define VLNK 5
#endif

struct packed_single {
	uint32_t length;
	attrreference_t name;
	fsobj_type_t objtype;
	char storage[256];
};

struct returned_attrs {
	uint32_t commonattr;
	uint32_t volattr;
	uint32_t dirattr;
	uint32_t fileattr;
	uint32_t forkattr;
};

static int check_single(const char* label, struct packed_single* packed, const char* want_name)
{
	char* name = ((char*)&packed->name) + packed->name.attr_dataoffset;

	if (packed->length < sizeof(*packed) - sizeof(packed->storage)) {
		fprintf(stderr, "%s: short packed length %u\n", label, packed->length);
		return 1;
	}
	if (packed->name.attr_length == 0 || strcmp(name, want_name) != 0) {
		fprintf(stderr, "%s: bad name '%s', want '%s'\n", label, name, want_name);
		return 1;
	}
	if (packed->objtype == 0) {
		fprintf(stderr, "%s: missing object type\n", label);
		return 1;
	}
	return 0;
}

static int check_getattrlist_family(void)
{
	const char* path = "/tmp/dar-getattrlist-packer-file";
	const char* want_name = "dar-getattrlist-packer-file";
	int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0644);
	if (fd < 0) {
		perror("open");
		return 1;
	}

	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	struct packed_single packed = {0};
	if (getattrlist(path, &attrs, &packed, sizeof(packed), 0) != 0) {
		fprintf(stderr, "getattrlist: %s\n", strerror(errno));
		return 1;
	}
	if (check_single("getattrlist", &packed, want_name))
		return 1;

	memset(&packed, 0, sizeof(packed));
	if (fgetattrlist(fd, &attrs, &packed, sizeof(packed), 0) != 0) {
		fprintf(stderr, "fgetattrlist: %s\n", strerror(errno));
		return 1;
	}
	if (check_single("fgetattrlist", &packed, want_name))
		return 1;

	memset(&packed, 0, sizeof(packed));
	if (getattrlistat(AT_FDCWD, path, &attrs, &packed, sizeof(packed), 0) != 0) {
		fprintf(stderr, "getattrlistat: %s\n", strerror(errno));
		return 1;
	}
	if (check_single("getattrlistat", &packed, want_name))
		return 1;

	close(fd);
	unlink(path);
	return 0;
}

static int check_root_name(void)
{
	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	struct packed_single packed = {0};
	if (getattrlist("/", &attrs, &packed, sizeof(packed), 0) != 0) {
		fprintf(stderr, "getattrlist root: %s\n", strerror(errno));
		return 1;
	}
	if (check_single("getattrlist root", &packed, "/"))
		return 1;

	int fd = open("/", O_RDONLY | O_DIRECTORY);
	if (fd < 0) {
		fprintf(stderr, "open root: %s\n", strerror(errno));
		return 1;
	}
	memset(&packed, 0, sizeof(packed));
	if (fgetattrlist(fd, &attrs, &packed, sizeof(packed), 0) != 0) {
		fprintf(stderr, "fgetattrlist root: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	if (check_single("fgetattrlist root", &packed, "/"))
		return 1;

	return 0;
}

static int check_error_paths(void)
{
	const char* path = "/tmp/dar-getattrlist-packer-error-file";
	int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0644);
	if (fd < 0) {
		perror("open error file");
		return 1;
	}
	close(fd);

	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	uint32_t tiny = 0;
	errno = 0;
	if (getattrlist(path, &attrs, &tiny, sizeof(tiny), 0) == 0 || errno != ERANGE) {
		fprintf(stderr, "getattrlist tiny buffer: rc/errno mismatch errno=%d %s\n", errno, strerror(errno));
		unlink(path);
		return 1;
	}

	tiny = 0;
	errno = 0;
	if (getattrlist(path, &attrs, &tiny, sizeof(tiny), FSOPT_REPORT_FULLSIZE) != 0) {
		fprintf(stderr, "getattrlist REPORT_FULLSIZE: %s\n", strerror(errno));
		unlink(path);
		return 1;
	}
	if (tiny <= sizeof(tiny)) {
		fprintf(stderr, "getattrlist REPORT_FULLSIZE: bad full length %u\n", tiny);
		unlink(path);
		return 1;
	}

	attrs.commonattr = ATTR_CMN_CRTIME;
	errno = 0;
	struct packed_single packed = {0};
	if (getattrlist(path, &attrs, &packed, sizeof(packed), 0) == 0 || errno != EINVAL) {
		fprintf(stderr, "getattrlist unsupported attr: rc/errno mismatch errno=%d %s\n", errno, strerror(errno));
		unlink(path);
		return 1;
	}

	unlink(path);
	return 0;
}

static int check_symlink_nofollow(void)
{
	const char* target = "/tmp/dar-getattrlist-packer-symlink-target";
	const char* link = "/tmp/dar-getattrlist-packer-symlink";
	unlink(link);
	unlink(target);
	int fd = open(target, O_CREAT | O_TRUNC | O_RDWR, 0644);
	if (fd < 0) {
		perror("open symlink target");
		return 1;
	}
	close(fd);
	if (symlink(target, link) != 0) {
		perror("symlink");
		unlink(target);
		return 1;
	}

	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	struct packed_single packed = {0};
	if (getattrlist(link, &attrs, &packed, sizeof(packed), FSOPT_NOFOLLOW) != 0) {
		fprintf(stderr, "getattrlist symlink nofollow: %s\n", strerror(errno));
		unlink(link);
		unlink(target);
		return 1;
	}
	if (check_single("getattrlist symlink", &packed, "dar-getattrlist-packer-symlink")) {
		unlink(link);
		unlink(target);
		return 1;
	}
	if (packed.objtype != VLNK) {
		fprintf(stderr, "getattrlist symlink: objtype=%u want VLNK=%u\n", packed.objtype, VLNK);
		unlink(link);
		unlink(target);
		return 1;
	}

	unlink(link);
	unlink(target);
	return 0;
}

static int check_bulk_error(void)
{
	const char* dir = "/tmp/dar-getattrlistbulk-packer-dir";
	const char* file = "/tmp/dar-getattrlistbulk-packer-dir/file";
	unlink(file);
	rmdir(dir);
	mkdir(dir, 0755);
	int fd = open(file, O_CREAT | O_TRUNC | O_RDWR, 0644);
	if (fd >= 0)
		close(fd);

	int dfd = open(dir, O_RDONLY | O_DIRECTORY);
	if (dfd < 0) {
		perror("open dir");
		return 1;
	}

	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_NAME | ATTR_CMN_OBJTYPE | ATTR_CMN_ERROR;

	char buffer[4096] = {0};
	int count = getattrlistbulk(dfd, &attrs, buffer, sizeof(buffer), 0);
	if (count <= 0) {
		fprintf(stderr, "getattrlistbulk: count=%d errno=%d %s\n", count, errno, strerror(errno));
		return 1;
	}

	uint32_t length = *(uint32_t*)buffer;
	struct returned_attrs* returned = (struct returned_attrs*)(buffer + sizeof(uint32_t));
	if (length <= sizeof(uint32_t) + sizeof(struct returned_attrs)) {
		fprintf(stderr, "getattrlistbulk: short record length %u\n", length);
		return 1;
	}
	if (!(returned->commonattr & ATTR_CMN_ERROR)) {
		fprintf(stderr, "getattrlistbulk: ATTR_CMN_ERROR not returned (common=0x%x)\n", returned->commonattr);
		return 1;
	}

	close(dfd);
	unlink(file);
	rmdir(dir);
	return 0;
}

static int check_bulk_resume(void)
{
	const char* dir = "/tmp/dar-getattrlistbulk-resume-dir";
	char path[128];
	for (int i = 0; i < 3; ++i) {
		snprintf(path, sizeof(path), "%s/file%d", dir, i);
		unlink(path);
	}
	rmdir(dir);
	mkdir(dir, 0755);
	for (int i = 0; i < 3; ++i) {
		snprintf(path, sizeof(path), "%s/file%d", dir, i);
		int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0644);
		if (fd >= 0)
			close(fd);
	}

	int dfd = open(dir, O_RDONLY | O_DIRECTORY);
	if (dfd < 0) {
		perror("open resume dir");
		return 1;
	}

	struct attrlist attrs = {0};
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_NAME | ATTR_CMN_OBJTYPE | ATTR_CMN_ERROR;

	char probe[4096] = {0};
	int first = getattrlistbulk(dfd, &attrs, probe, sizeof(probe), 0);
	if (first < 3) {
		fprintf(stderr, "getattrlistbulk resume probe: count=%d errno=%d %s\n", first, errno, strerror(errno));
		close(dfd);
		return 1;
	}
	uint32_t first_len = *(uint32_t*)probe;
	if (first_len == 0 || first_len >= sizeof(probe)) {
		fprintf(stderr, "getattrlistbulk resume probe: bad first len %u\n", first_len);
		close(dfd);
		return 1;
	}

	lseek(dfd, 0, SEEK_SET);
	char small[512] = {0};
	int total = 0;
	for (int i = 0; i < 4; ++i) {
		memset(small, 0, sizeof(small));
		int count = getattrlistbulk(dfd, &attrs, small, first_len, 0);
		if (count < 0) {
			fprintf(stderr, "getattrlistbulk resume chunk: count=%d errno=%d %s\n", count, errno, strerror(errno));
			close(dfd);
			return 1;
		}
		if (count == 0)
			break;
		if (count != 1) {
			fprintf(stderr, "getattrlistbulk resume chunk: count=%d want 1\n", count);
			close(dfd);
			return 1;
		}
		total += count;
	}
	if (total != 3) {
		fprintf(stderr, "getattrlistbulk resume total=%d want 3\n", total);
		close(dfd);
		return 1;
	}

	close(dfd);
	for (int i = 0; i < 3; ++i) {
		snprintf(path, sizeof(path), "%s/file%d", dir, i);
		unlink(path);
	}
	rmdir(dir);
	return 0;
}

int main(void)
{
	if (check_getattrlist_family())
		return 1;
	if (check_root_name())
		return 1;
	if (check_error_paths())
		return 1;
	if (check_symlink_nofollow())
		return 1;
	if (check_bulk_error())
		return 1;
	if (check_bulk_resume())
		return 1;

	puts("GETATTRLIST_SHARED_PACKER_OK");
	return 0;
}
