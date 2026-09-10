#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>

static void require(int condition, const char *message)
{
	if (!condition) {
		fprintf(stderr, "LOCALHOST_RESOLUTION_FAILED: %s\n", message);
		exit(1);
	}
}

static void check_lookup(int family, int flags)
{
	struct addrinfo hints = {0}, *result = NULL;
	hints.ai_family = family;
	hints.ai_socktype = SOCK_STREAM;
	hints.ai_flags = flags;
	int error = getaddrinfo("localhost", "4242", &hints, &result);
	if (error) {
		fprintf(stderr, "getaddrinfo family=%d flags=%d: %s (%d)\n", family, flags, gai_strerror(error), error);
		exit(1);
	}
	require(result != NULL, "successful lookup returned no address");
	for (struct addrinfo *entry = result; entry; entry = entry->ai_next) {
		require(family == AF_UNSPEC || entry->ai_family == family, "requested address family");
		if (entry->ai_family == AF_INET) {
			require(entry->ai_addrlen >= sizeof(struct sockaddr_in), "complete IPv4 address");
			const struct sockaddr_in *address = (const void *)entry->ai_addr;
			require((ntohl(address->sin_addr.s_addr) >> 24) == 127, "IPv4 loopback address");
			require(address->sin_port == htons(4242), "IPv4 service port");
		} else {
			require(entry->ai_family == AF_INET6 && entry->ai_addrlen >= sizeof(struct sockaddr_in6), "complete IPv6 address");
			const struct sockaddr_in6 *address = (const void *)entry->ai_addr;
			require(IN6_IS_ADDR_LOOPBACK(&address->sin6_addr), "IPv6 loopback address");
			require(address->sin6_port == htons(4242), "IPv6 service port");
		}
	}
	freeaddrinfo(result);
}

int main(void)
{
	check_lookup(AF_INET, 0);
	check_lookup(AF_INET6, 0);
	check_lookup(AF_UNSPEC, AI_ADDRCONFIG);
	puts("LOCALHOST_RESOLUTION_OK");
	return 0;
}
