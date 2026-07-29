#pragma once

#include <cstdint>

struct dtape_thread;
struct dtape_semaphore;
struct dtape_task;
struct dtape_kqchan_mach_port;

using dtape_thread_t = dtape_thread;
using dtape_semaphore_t = dtape_semaphore;
using dtape_task_t = dtape_task;
using dtape_kqchan_mach_port_t = dtape_kqchan_mach_port;
using libsimple_lock_t = int;

enum dtape_semaphore_wait_result_t {
	dtape_semaphore_wait_result_ok,
	dtape_semaphore_wait_result_interrupted,
	dtape_semaphore_wait_result_timed_out,
};

enum {
	dtape_thread_state_dead = 0,
	dtape_thread_state_running = 1,
	dtape_thread_state_stopped = 2,
	dtape_thread_state_interruptible = 3,
	dtape_thread_state_uninterruptible = 4,
};

std::uint32_t dtape_mach_reply_port();
bool dtape_thread_clear_stranded_wait(dtape_thread_t*);
inline dtape_semaphore_wait_result_t dtape_semaphore_down_timeout(
		dtape_semaphore_t*, unsigned int) {
	return dtape_semaphore_wait_result_ok;
}
