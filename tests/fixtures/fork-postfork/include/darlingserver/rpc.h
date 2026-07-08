#pragma once
void __dserver_per_thread_socket_refresh(void);
int __dserver_process_lifetime_pipe_refresh(void);
int __dserver_per_thread_socket(void);
int __dserver_get_process_lifetime_pipe(void);
void __dserver_close_socket(int fd);
void __dserver_close_process_lifetime_pipe(int fd);
int dserver_rpc_checkin(int fork_child, void* stack_addr, int lifetime_pipe);
