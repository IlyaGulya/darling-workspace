#pragma once
struct simple_readline_buf { int unused; };
void __simple_readline_init(struct simple_readline_buf *buf);
int __simple_readline(int fd, struct simple_readline_buf *buf, char *line, unsigned long size);
