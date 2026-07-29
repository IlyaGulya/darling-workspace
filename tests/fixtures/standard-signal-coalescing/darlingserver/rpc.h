#pragma once

enum dserver_rpc_architecture_t {
	dserver_rpc_architecture_invalid,
	dserver_rpc_architecture_i386,
	dserver_rpc_architecture_x86_64,
	dserver_rpc_architecture_arm32,
	dserver_rpc_architecture_arm64,
};

using dserver_s2c_msgnum_t = unsigned int;
