if(NOT TEST_NAME)
  set(TEST_NAME "unknown")
endif()
message(FATAL_ERROR
  "${TEST_NAME}: DARLING_LAUNCHER is unset. Run through `west test --prefix ...`, "
  "set DARLING/DARLING_LAUNCHER, or configure with -DDARLING_LAUNCHER=<launcher>.")
