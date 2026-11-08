#pragma once
/* GNU host compilers predefine linux; the guest header uses it as a parameter. */
#pragma push_macro("linux")
#undef linux
#include_next <conversion/signal/duct_signals.h>
#pragma pop_macro("linux")
