#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/attr.h>
#include <sys/stat.h>
#include <sys/vnode.h>
#include <unistd.h>

struct seen_entries {
	int file0;
	int file1;
	int file2;
	int subdir;
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
create_file(const char *path)
{
	FILE *file = fopen(path, "w");
	if (file == NULL) {
		fail_errno("fopen");
	}
	fputs("payload\n", file);
	if (fclose(file) != 0) {
		fail_errno("fclose");
	}
}

static void
mark_seen(struct seen_entries *seen, const char *name, fsobj_type_t objtype)
{
	if (strcmp(name, "file0") == 0 && objtype == VREG) {
		seen->file0 = 1;
	} else if (strcmp(name, "file1") == 0 && objtype == VREG) {
		seen->file1 = 1;
	} else if (strcmp(name, "file2") == 0 && objtype == VREG) {
		seen->file2 = 1;
	} else if (strcmp(name, "subdir") == 0 && objtype == VDIR) {
		seen->subdir = 1;
	} else {
		fprintf(stderr, "unexpected entry name=%s objtype=%u\n", name, objtype);
		exit(1);
	}
}

static uint32_t
parse_record(const char *base, size_t available, struct seen_entries *seen)
{
	if (available < sizeof(uint32_t)) {
		fprintf(stderr, "record buffer too short: %zu\n", available);
		exit(1);
	}

	uint32_t length;
	memcpy(&length, base, sizeof(length));
	if (length < sizeof(uint32_t) + sizeof(attribute_set_t) +
			sizeof(attrreference_t) + sizeof(fsobj_type_t)) {
		fprintf(stderr, "short record length %u\n", length);
		exit(1);
	}
	if (length > available) {
		fprintf(stderr, "record length %u exceeds available %zu\n", length, available);
		exit(1);
	}

	attribute_set_t returned;
	memcpy(&returned, base + sizeof(uint32_t), sizeof(returned));
	if (!(returned.commonattr & ATTR_CMN_RETURNED_ATTRS) ||
			!(returned.commonattr & ATTR_CMN_NAME) ||
			!(returned.commonattr & ATTR_CMN_OBJTYPE)) {
		fprintf(stderr, "bad returned attrs common=0x%x\n", returned.commonattr);
		exit(1);
	}

	const char *field = base + sizeof(uint32_t) + sizeof(attribute_set_t);
	const char *name_ref_base = field;
	attrreference_t name_ref;
	memcpy(&name_ref, field, sizeof(name_ref));
	field += sizeof(name_ref);

	fsobj_type_t objtype;
	memcpy(&objtype, field, sizeof(objtype));

	ptrdiff_t name_start = (name_ref_base - base) + name_ref.attr_dataoffset;
	if (name_ref.attr_length == 0 ||
			name_ref.attr_dataoffset < 0 ||
			name_start < 0 ||
			(uint32_t)name_start > length ||
			name_ref.attr_length > length - (uint32_t)name_start) {
		fprintf(stderr, "name reference out of bounds offset=%d length=%u record=%u\n",
			name_ref.attr_dataoffset, name_ref.attr_length, length);
		exit(1);
	}

	const char *name = base + name_start;
	if (name[name_ref.attr_length - 1] != '\0') {
		fprintf(stderr, "name is not NUL-terminated\n");
		exit(1);
	}

	mark_seen(seen, name, objtype);
	return length;
}

static uint32_t
check_full_bulk(int dfd, struct attrlist *attrs)
{
	char buffer[4096];
	memset(buffer, 0, sizeof(buffer));

	if (lseek(dfd, 0, SEEK_SET) < 0) {
		fail_errno("lseek");
	}

	int count = getattrlistbulk(dfd, attrs, buffer, sizeof(buffer), 0);
	if (count != 4) {
		fprintf(stderr, "full count=%d errno=%d %s, want 4\n", count, errno, strerror(errno));
		exit(1);
	}

	struct seen_entries seen = {0};
	size_t offset = 0;
	uint32_t max_record_length = 0;
	for (int i = 0; i < count; ++i) {
		uint32_t length = parse_record(buffer + offset, sizeof(buffer) - offset, &seen);
		if (length > max_record_length) {
			max_record_length = length;
		}
		offset += length;
	}

	if (!seen.file0 || !seen.file1 || !seen.file2 || !seen.subdir) {
		fprintf(stderr, "missing entries file0=%d file1=%d file2=%d subdir=%d\n",
			seen.file0, seen.file1, seen.file2, seen.subdir);
		exit(1);
	}
	return max_record_length;
}

static void
check_small_buffer_resume(int dfd, struct attrlist *attrs, uint32_t record_length)
{
	char buffer[512];
	if (record_length > sizeof(buffer)) {
		fprintf(stderr, "record too large for resume buffer: %u\n", record_length);
		exit(1);
	}

	if (lseek(dfd, 0, SEEK_SET) < 0) {
		fail_errno("lseek");
	}

	struct seen_entries seen = {0};
	int total = 0;
	for (int i = 0; i < 5; ++i) {
		memset(buffer, 0, sizeof(buffer));
		int count = getattrlistbulk(dfd, attrs, buffer, record_length, 0);
		if (count < 0) {
			fprintf(stderr, "resume count=%d errno=%d %s\n", count, errno, strerror(errno));
			exit(1);
		}
		if (count == 0) {
			break;
		}
		if (count != 1) {
			fprintf(stderr, "resume returned %d records, want 1\n", count);
			exit(1);
		}
		parse_record(buffer, record_length, &seen);
		total += count;
	}

	if (total != 4 || !seen.file0 || !seen.file1 || !seen.file2 || !seen.subdir) {
		fprintf(stderr, "bad resume total=%d file0=%d file1=%d file2=%d subdir=%d\n",
			total, seen.file0, seen.file1, seen.file2, seen.subdir);
		exit(1);
	}
}

int
main(void)
{
	char dir_template[] = "/tmp/getattrlistbulk-guest.XXXXXX";
	char *dir = mkdtemp(dir_template);
	if (dir == NULL) {
		fail_errno("mkdtemp");
	}

	char file0[512];
	char file1[512];
	char file2[512];
	char subdir[512];
	checked_join(file0, sizeof(file0), dir, "file0");
	checked_join(file1, sizeof(file1), dir, "file1");
	checked_join(file2, sizeof(file2), dir, "file2");
	checked_join(subdir, sizeof(subdir), dir, "subdir");

	create_file(file0);
	create_file(file1);
	create_file(file2);
	if (mkdir(subdir, 0700) != 0) {
		fail_errno("mkdir");
	}

	int dfd = open(dir, O_RDONLY | O_DIRECTORY);
	if (dfd < 0) {
		fail_errno("open");
	}

	struct attrlist attrs;
	memset(&attrs, 0, sizeof(attrs));
	attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
	attrs.commonattr = ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_NAME | ATTR_CMN_OBJTYPE;

	uint32_t max_record_length = check_full_bulk(dfd, &attrs);
	check_small_buffer_resume(dfd, &attrs, max_record_length);

	close(dfd);
	unlink(file0);
	unlink(file1);
	unlink(file2);
	rmdir(subdir);
	rmdir(dir);

	puts("GETATTRLISTBULK_GUEST_OK");
	return 0;
}
