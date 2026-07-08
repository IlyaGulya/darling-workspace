#pragma once
struct linux_sockaddr_un {
	unsigned short sun_family;
	char sun_path[108];
};
