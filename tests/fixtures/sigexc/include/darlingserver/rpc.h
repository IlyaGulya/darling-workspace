#pragma once
int dserver_rpc_interrupt_enter(void);
int dserver_rpc_interrupt_exit(void);
int dserver_rpc_thread_suspended(void* thread_state, void* float_state);
int dserver_rpc_s2c_perform(void);
int dserver_rpc_sigprocess(int bsd_signum_in, int linux_signum, int sender_pid,
	int code, void* fault_addr, void* thread_state, void* float_state,
	int* bsd_signum_out);
