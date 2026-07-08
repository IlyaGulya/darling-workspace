#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <darling/emulation/xnu_syscall/bsd/helper/xattr/getattrlist_pack.h>

static int expect_int(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

static int expect_true(const char* label, int value)
{
	if (value)
		return 0;
	fprintf(stderr, "%s: condition is false\n", label);
	return 1;
}

static int check_name_objtype_record(void)
{
	struct xnu_attrlist attrs = {0};
	attrs.bitmapcount = DARLING_ATTR_BIT_MAP_COUNT;
	attrs.commonattr = DARLING_ATTR_CMN_NAME | DARLING_ATTR_CMN_OBJTYPE;

	struct darling_attrpack_entry entry = {0};
	entry.name = "alpha.txt";
	entry.name_len = strlen(entry.name);
	entry.objtype = DARLING_DTAPE_VREG;
	entry.has_objtype = 1;

	const size_t expected =
		sizeof(uint32_t) +
		sizeof(struct darling_attrreference) +
		sizeof(uint32_t) +
		darling_attr_align4(entry.name_len + 1);
	if (expect_int("entry size", (long)darling_attrpack_entry_size(&attrs, &entry), (long)expected))
		return 1;

	unsigned char buffer[128] = {0x5a};
	size_t written = darling_attrpack_entry((char*)buffer, &attrs, &entry);
	if (expect_int("written size", (long)written, (long)expected))
		return 1;

	uint32_t length = 0;
	memcpy(&length, buffer, sizeof(length));
	if (expect_int("record length field", length, (long)expected))
		return 1;

	struct darling_attrreference ref;
	memcpy(&ref, buffer + sizeof(uint32_t), sizeof(ref));
	if (expect_int("name length", ref.attr_length, (long)entry.name_len + 1))
		return 1;

	const unsigned char* objtype_ptr = buffer + sizeof(uint32_t) + sizeof(ref);
	uint32_t objtype = 0;
	memcpy(&objtype, objtype_ptr, sizeof(objtype));
	if (expect_int("objtype", objtype, DARLING_DTAPE_VREG))
		return 1;

	const char* packed_name = (const char*)(buffer + sizeof(uint32_t) + ref.attr_dataoffset);
	if (expect_true("packed name matches basename", strcmp(packed_name, entry.name) == 0))
		return 1;

	size_t name_end = sizeof(uint32_t) + ref.attr_dataoffset + entry.name_len + 1;
	for (size_t i = name_end; i < written; ++i) {
		if (buffer[i] != 0) {
			fprintf(stderr, "name padding byte %zu is %#x, want 0\n", i, buffer[i]);
			return 1;
		}
	}
	return 0;
}

static int check_returned_attrs_subset(void)
{
	struct xnu_attrlist attrs = {0};
	attrs.bitmapcount = DARLING_ATTR_BIT_MAP_COUNT;
	attrs.commonattr = DARLING_ATTR_CMN_RETURNED_ATTRS |
		DARLING_ATTR_CMN_NAME |
		DARLING_ATTR_CMN_OBJTYPE |
		DARLING_ATTR_CMN_ERROR;
	attrs.dirattr = DARLING_ATTR_DIR_ENTRYCOUNT;
	attrs.fileattr = DARLING_ATTR_FILE_RSRCLENGTH;

	struct darling_attrpack_entry entry = {0};
	entry.name = "directory";
	entry.name_len = strlen(entry.name);
	entry.objtype = DARLING_DTAPE_VDIR;
	entry.error = 37;
	entry.dir_entrycount = 9;
	entry.has_objtype = 1;
	entry.has_dir_entrycount = 1;

	unsigned char buffer[160] = {0};
	size_t written = darling_attrpack_entry((char*)buffer, &attrs, &entry);
	uint32_t length = 0;
	memcpy(&length, buffer, sizeof(length));
	if (expect_int("returned record length", length, (long)written))
		return 1;

	struct darling_attribute_set returned;
	memcpy(&returned, buffer + sizeof(uint32_t), sizeof(returned));
	if (expect_true("returned common includes returned attrs",
			returned.commonattr & DARLING_ATTR_CMN_RETURNED_ATTRS))
		return 1;
	if (expect_true("returned common includes name",
			returned.commonattr & DARLING_ATTR_CMN_NAME))
		return 1;
	if (expect_true("returned common includes objtype",
			returned.commonattr & DARLING_ATTR_CMN_OBJTYPE))
		return 1;
	if (expect_true("returned common includes error",
			returned.commonattr & DARLING_ATTR_CMN_ERROR))
		return 1;
	if (expect_true("returned dir includes entrycount",
			returned.dirattr & DARLING_ATTR_DIR_ENTRYCOUNT))
		return 1;
	if (expect_true("returned file omits missing rsrc length",
			!(returned.fileattr & DARLING_ATTR_FILE_RSRCLENGTH)))
		return 1;

	const unsigned char* p = buffer + sizeof(uint32_t) + sizeof(returned);
	const unsigned char* name_ref_ptr = p;
	struct darling_attrreference ref;
	memcpy(&ref, p, sizeof(ref));
	p += sizeof(ref);
	uint32_t objtype = 0;
	memcpy(&objtype, p, sizeof(objtype));
	p += sizeof(objtype);
	uint32_t error = 0;
	memcpy(&error, p, sizeof(error));
	p += sizeof(error);
	uint32_t entrycount = 0;
	memcpy(&entrycount, p, sizeof(entrycount));

	if (expect_int("returned objtype", objtype, DARLING_DTAPE_VDIR))
		return 1;
	if (expect_int("returned error value", error, 37))
		return 1;
	if (expect_int("returned entrycount", entrycount, 9))
		return 1;
	const char* packed_name = (const char*)(name_ref_ptr + ref.attr_dataoffset);
	if (expect_true("returned packed name", strcmp(packed_name, entry.name) == 0))
		return 1;
	return 0;
}

static int check_bulk_style_continuation_records(void)
{
	struct xnu_attrlist attrs = {0};
	attrs.bitmapcount = DARLING_ATTR_BIT_MAP_COUNT;
	attrs.commonattr = DARLING_ATTR_CMN_RETURNED_ATTRS |
		DARLING_ATTR_CMN_NAME |
		DARLING_ATTR_CMN_OBJTYPE |
		DARLING_ATTR_CMN_ERROR;

	struct darling_attrpack_entry entries[3] = {0};
	for (size_t i = 0; i < 3; ++i) {
		static const char* names[] = {"file0", "file1", "file2"};
		entries[i].name = names[i];
		entries[i].name_len = strlen(names[i]);
		entries[i].objtype = DARLING_DTAPE_VREG;
		entries[i].has_objtype = 1;
	}

	unsigned char buffer[512] = {0};
	size_t offset = 0;
	for (size_t i = 0; i < 3; ++i) {
		size_t written = darling_attrpack_entry((char*)buffer + offset, &attrs, &entries[i]);
		if (expect_true("bulk-style record makes progress", written > sizeof(uint32_t)))
			return 1;
		uint32_t length = 0;
		memcpy(&length, buffer + offset, sizeof(length));
		if (expect_int("bulk-style length prefix", length, (long)written))
			return 1;
		offset += written;
	}

	size_t scan = 0;
	for (size_t i = 0; i < 3; ++i) {
		uint32_t length = 0;
		memcpy(&length, buffer + scan, sizeof(length));
		if (expect_true("bulk-style continuation length", length > sizeof(uint32_t)))
			return 1;
		struct darling_attribute_set returned;
		memcpy(&returned, buffer + scan + sizeof(uint32_t), sizeof(returned));
		if (expect_true("bulk-style returned attrs present",
				returned.commonattr & DARLING_ATTR_CMN_RETURNED_ATTRS))
			return 1;
		scan += length;
	}

	if (expect_int("bulk-style scan reaches end", (long)scan, (long)offset))
		return 1;
	return 0;
}

static int check_validation_and_type_mapping(void)
{
	struct xnu_attrlist attrs = {0};
	attrs.bitmapcount = DARLING_ATTR_BIT_MAP_COUNT;
	attrs.commonattr = DARLING_ATTR_CMN_NAME;
	if (expect_int("valid attrlist", darling_attrlist_validate(&attrs, 0), 0))
		return 1;

	attrs.commonattr = 0x00000200;
	if (expect_int("unsupported attr rejected", darling_attrlist_validate(&attrs, 0), -EINVAL))
		return 1;

	attrs.commonattr = DARLING_ATTR_CMN_RETURNED_ATTRS | 0x00000200;
	if (expect_int("returned subset allowed", darling_attrlist_validate(&attrs, 1), 0))
		return 1;

	if (expect_int("regular mode objtype", darling_objtype_from_mode(DARLING_LINUX_S_IFREG), DARLING_DTAPE_VREG))
		return 1;
	if (expect_int("directory dtype objtype", darling_objtype_from_dtype(DARLING_LINUX_DT_DIR), DARLING_DTAPE_VDIR))
		return 1;
	if (expect_int("unknown dtype objtype", darling_objtype_from_dtype(255), DARLING_DTAPE_VNON))
		return 1;
	return 0;
}

int main(void)
{
	if (check_name_objtype_record())
		return 1;
	if (check_returned_attrs_subset())
		return 1;
	if (check_bulk_style_continuation_records())
		return 1;
	if (check_validation_and_type_mapping())
		return 1;
	puts("GREEN: getattrlist shared packer contract");
	return 0;
}
