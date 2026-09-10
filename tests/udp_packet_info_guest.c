#define __APPLE_USE_RFC_3542 1
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static void check(int condition, const char *operation)
{
	if (!condition) {
		fprintf(stderr, "%s: errno=%d (%s)\n", operation, errno, strerror(errno));
		exit(1);
	}
}

static void roundtrip(int family)
{
	int receiver = socket(family, SOCK_DGRAM, 0);
	int sender = socket(family, SOCK_DGRAM, 0);
	check(receiver >= 0 && sender >= 0, "socket");
	struct sockaddr_storage address = {0};
	socklen_t address_length;
	if (family == AF_INET) {
		struct sockaddr_in *v4 = (void *)&address;
		v4->sin_family = AF_INET;
		v4->sin_len = sizeof(*v4);
		v4->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
		address_length = sizeof(*v4);
	} else {
		struct sockaddr_in6 *v6 = (void *)&address;
		v6->sin6_family = AF_INET6;
		v6->sin6_len = sizeof(*v6);
		v6->sin6_addr = in6addr_loopback;
		address_length = sizeof(*v6);
	}
	check(bind(receiver, (void *)&address, address_length) == 0, "bind");
	check(getsockname(receiver, (void *)&address, &address_length) == 0, "getsockname");
	int enabled = 1;
	int level = family == AF_INET ? IPPROTO_IP : IPPROTO_IPV6;
	int option = family == AF_INET ? IP_PKTINFO : IPV6_RECVPKTINFO;
	check(setsockopt(receiver, level, option, &enabled, sizeof(enabled)) == 0, "enable packet info");
	int observed = 0;
	socklen_t observed_length = sizeof(observed);
	check(getsockopt(receiver, level, option, &observed, &observed_length) == 0 && observed == 1, "packet info enabled state");
	struct timeval timeout = { .tv_sec = 2 };
	check(setsockopt(receiver, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) == 0, "receive timeout");

	union { struct cmsghdr alignment; unsigned char bytes[128]; } outgoing = {0}, incoming = {0};
	char payload = 'P', received = 0;
	struct iovec send_iov = { &payload, sizeof(payload) };
	struct msghdr send_message = {0};
	send_message.msg_name = &address;
	send_message.msg_namelen = address_length;
	send_message.msg_iov = &send_iov;
	send_message.msg_iovlen = 1;
	send_message.msg_control = outgoing.bytes;
	size_t info_size = family == AF_INET ? sizeof(struct in_pktinfo) : sizeof(struct in6_pktinfo);
	send_message.msg_controllen = CMSG_SPACE(info_size);
	struct cmsghdr *control = CMSG_FIRSTHDR(&send_message);
	control->cmsg_len = CMSG_LEN(info_size);
	control->cmsg_level = level;
	control->cmsg_type = family == AF_INET ? IP_PKTINFO : IPV6_PKTINFO;
	if (family == AF_INET) {
		struct in_pktinfo *info = (void *)CMSG_DATA(control);
		/* A non-default loopback source detects silently discarded send metadata. */
		info->ipi_spec_dst.s_addr = htonl(INADDR_LOOPBACK + 1);
	} else {
		struct in6_pktinfo *info = (void *)CMSG_DATA(control);
		info->ipi6_addr = in6addr_loopback;
	}
	unsigned char original_control[128];
	memcpy(original_control, outgoing.bytes, sizeof(original_control));
	check(sendmsg(sender, &send_message, 0) == sizeof(payload), "send source packet info");
	check(memcmp(original_control, outgoing.bytes, sizeof(original_control)) == 0, "sendmsg preserves caller control data");
	struct iovec receive_iov = { &received, sizeof(received) };
	struct sockaddr_storage peer = {0};
	struct msghdr receive_message = {0};
	receive_message.msg_name = &peer;
	receive_message.msg_namelen = sizeof(peer);
	receive_message.msg_iov = &receive_iov;
	receive_message.msg_iovlen = 1;
	receive_message.msg_control = incoming.bytes;
	receive_message.msg_controllen = sizeof(incoming.bytes);
	check(recvmsg(receiver, &receive_message, 0) == sizeof(received) && received == payload, "receive payload");
	check(!(receive_message.msg_flags & MSG_CTRUNC), "complete packet info");
	if (family == AF_INET)
		check(((struct sockaddr_in *)&peer)->sin_addr.s_addr == htonl(INADDR_LOOPBACK + 1), "requested IPv4 source address");
	else
		check(IN6_IS_ADDR_LOOPBACK(&((struct sockaddr_in6 *)&peer)->sin6_addr), "IPv6 peer address");
	int found = 0;
	for (control = CMSG_FIRSTHDR(&receive_message); control; control = CMSG_NXTHDR(&receive_message, control)) {
		if (control->cmsg_level != level || control->cmsg_type != (family == AF_INET ? IP_PKTINFO : IPV6_PKTINFO))
			continue;
		check(control->cmsg_len == CMSG_LEN(info_size), "packet info size");
		if (family == AF_INET) {
			struct in_pktinfo *info = (void *)CMSG_DATA(control);
			check(info->ipi_addr.s_addr == htonl(INADDR_LOOPBACK) && info->ipi_ifindex != 0, "IPv4 destination and interface");
		} else {
			struct in6_pktinfo *info = (void *)CMSG_DATA(control);
			check(IN6_IS_ADDR_LOOPBACK(&info->ipi6_addr) && info->ipi6_ifindex != 0, "IPv6 destination and interface");
		}
		found++;
	}
	check(found == 1, "one matching packet-info message");
	enabled = 0;
	check(setsockopt(receiver, level, option, &enabled, sizeof(enabled)) == 0, "disable packet info");
	observed = 1;
	check(getsockopt(receiver, level, option, &observed, &observed_length) == 0 && observed == 0, "packet info disabled state");
	close(sender);
	close(receiver);
}

int main(void)
{
	roundtrip(AF_INET);
	roundtrip(AF_INET6);
	puts("UDP_PACKET_INFO_OK");
	return 0;
}
