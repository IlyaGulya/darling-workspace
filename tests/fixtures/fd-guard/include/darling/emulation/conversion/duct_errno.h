#pragma once

/*
 * Production builds provide the Darwin errno namespace through their normal
 * umbrella headers.  The standalone c-fixture must model that include context
 * explicitly so EBADF in dup2.c is a declared errno constant, rather than
 * accidentally depending on a compiler preinclude.
 */
#include <errno.h>
