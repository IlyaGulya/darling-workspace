/* Compatibility symbol for the RED source tree, whose production unit predates
 * the TEST-only whiteout failure hook. The fixed tree provides the strong
 * definition from vchroot_userspace.c; this weak fallback then disappears. */
int eunion_test_fail_whiteout __attribute__((weak));
int eunion_test_fail_xattr __attribute__((weak));
