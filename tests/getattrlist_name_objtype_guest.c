#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/attr.h>
#include <sys/stat.h>
#include <sys/vnode.h>
#include <unistd.h>

struct name_objtype_record {
	uint32_t length;
	attrreference_t name;
	fsobj_type_t objtype;
	char storage[256];
};

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

static void
checked_join(char *dst, size_t dst_size, const char *dir, const char *leaf)
{
	int length = snprintf(dst, dst_size, "%s/%s", dir, leaf);
	if (length < 0 || (size_t)length >= dst_size) {
		fprintf(stderr, "path too long: %s/%s\n", dir, leaf);
		exit(1);
	}
}

static void
check_getattrlist(const char *path, const char *expected_name,
	fsobj_type_t expected_type, unsigned long options)
{
	struct attrlist attrs;
	memset(&attrs, 0, sizeof(attrs));
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	struct name_objtype_record record;
	memset(&record, 0xa5, sizeof(record));

	if (getattrlist(path, &attrs, &record, sizeof(record), options) != 0) {
		fail_errno("getattrlist");
	}

	if (record.length < sizeof(record.length) + sizeof(record.name) + sizeof(record.objtype)) {
		fprintf(stderr, "%s: short record length %u\n", path, record.length);
		exit(1);
	}
	if (record.length > sizeof(record)) {
		fprintf(stderr, "%s: oversized record length %u\n", path, record.length);
		exit(1);
	}
	if (record.name.attr_length != strlen(expected_name) + 1) {
		fprintf(stderr, "%s: name length=%u expected=%zu\n",
			path, record.name.attr_length, strlen(expected_name) + 1);
		exit(1);
	}
	if (record.name.attr_dataoffset < 0 ||
		(uint32_t)record.name.attr_dataoffset > record.length ||
		record.name.attr_length > record.length - (uint32_t)record.name.attr_dataoffset) {
		fprintf(stderr, "%s: name reference out of bounds offset=%d length=%u record=%u\n",
			path, record.name.attr_dataoffset, record.name.attr_length, record.length);
		exit(1);
	}

	const char *name = (const char *)(&record.name) + record.name.attr_dataoffset;
	if (name[record.name.attr_length - 1] != '\0') {
		fprintf(stderr, "%s: name is not NUL-terminated\n", path);
		exit(1);
	}
	if (strcmp(name, expected_name) != 0) {
		fprintf(stderr, "%s: name='%s' expected='%s'\n", path, name, expected_name);
		exit(1);
	}
	if (record.objtype != expected_type) {
		fprintf(stderr, "%s: objtype=%u expected=%u\n",
			path, record.objtype, expected_type);
		exit(1);
	}
}

int
main(void)
{
	char dir_template[] = "/tmp/getattrlist-name-objtype.XXXXXX";
	char *dir = mkdtemp(dir_template);
	if (dir == NULL) {
		fail_errno("mkdtemp");
	}

	char file_path[512];
	char subdir_path[512];
	char link_path[512];
	checked_join(file_path, sizeof(file_path), dir, "alpha.txt");
	checked_join(subdir_path, sizeof(subdir_path), dir, "subdir");
	checked_join(link_path, sizeof(link_path), dir, "link-to-alpha");

	FILE *file = fopen(file_path, "w");
	if (file == NULL) {
		fail_errno("fopen");
	}
	fputs("alpha\n", file);
	fclose(file);

	if (mkdir(subdir_path, 0700) != 0) {
		fail_errno("mkdir");
	}
	if (symlink("alpha.txt", link_path) != 0) {
		fail_errno("symlink");
	}

	check_getattrlist(file_path, "alpha.txt", VREG, 0);
	check_getattrlist(subdir_path, "subdir", VDIR, 0);
	check_getattrlist(link_path, "link-to-alpha", VLNK, FSOPT_NOFOLLOW);

	unlink(link_path);
	unlink(file_path);
	rmdir(subdir_path);
	rmdir(dir);

	puts("GETATTRLIST_NAME_OBJTYPE_GUEST_OK");
	return 0;
}
