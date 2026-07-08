#pragma once
static inline void __simple_printf(const char* message, ...) { (void)message; }
static inline void __simple_abort(void) { __builtin_trap(); }
