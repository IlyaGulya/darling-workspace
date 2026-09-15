#!/usr/bin/env python3
"""Temporary stock-source experiment; West owns the prefix and diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv
from stock_replay_program import resolve_wget_iterations, wget_repeat_phase

prefix = Path(os.environ["DPREFIX"])
wget_iterations, wget_acceptance = resolve_wget_iterations(os.environ)
launcher = os.environ["DARLING_LAUNCHER"]
phase = os.environ.get("WEST_STOCK_REPLAY_PHASE", "install")
core = "ad6d3bbf8f5eac27a5ce90e695c6b41765d40bb7"
archive = Path(os.environ["WEST_STOCK_CORE_ARCHIVE"])
with archive.open("rb") as stream:
    core_sha = hashlib.file_digest(stream, "sha256").hexdigest()
if core_sha != "bbb58677b5b5850620348fab4b37debe57fd3bea7adcbaa4a64f6abb5ed52f39":
    raise SystemExit("stock core differs from the previously accepted archive")
works = [path.parent for path in (prefix / "private/var/tmp").glob("west-homebrew-lz4-*/inputs.json")
         if json.loads(path.read_text()).get("homebrew-core-commit") == core
         and (path.parent / "reuse-roundtrip.ok").is_file()]
if len(works) != 1:
    raise SystemExit(f"expected one completed stock Lz4 resource, found {len(works)}")
work = works[0]
tap = prefix / "usr/local/Homebrew/Library/Taps/homebrew/homebrew-core"
print("STOCK_CORE", core, core_sha, "phase=" + phase, flush=True)
with tempfile.TemporaryDirectory(prefix="west-stock-core-") as scratch:
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(scratch, filter="data")
    tree = Path(scratch) / f"homebrew-core-{core}"
    for item in tree.rglob("*"):
        destination = tap / item.relative_to(tree)
        if item.is_symlink():
            if destination.exists() or destination.is_symlink():
                if not destination.is_symlink() or item.readlink() != destination.readlink():
                    raise SystemExit(f"non-stock tap alias: {destination}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(item.readlink())
        elif item.is_file():
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file() or item.read_bytes() != destination.read_bytes():
                    raise SystemExit(f"non-stock tap file: {destination}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
        elif destination.is_symlink():
            raise SystemExit(f"unexpected tap directory symlink: {destination}")
        else:
            destination.mkdir(parents=True, exist_ok=True)
wget_archive = Path(os.environ["WEST_STOCK_WGET_ARCHIVE"])
with wget_archive.open("rb") as stream:
    wget_sha = hashlib.file_digest(stream, "sha256").hexdigest()
if wget_sha != "766e48423e79359ea31e41db9e5c289675947a7fcf2efdcedb726ac9d0da3784":
    raise SystemExit("stock wget archive checksum mismatch")
wget_url = "https://ftpmirror.gnu.org/gnu/wget/wget-1.25.0.tar.gz"
shutil.copyfile(wget_archive, work / "cache/downloads" / (
    hashlib.sha256(wget_url.encode()).hexdigest() + "--wget-1.25.0.tar.gz"))
state = os.environ.get("WEST_JOB_STATE_DIR")
if state:
    directory = Path(state) / "activity-logs.d"
    directory.mkdir(exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="stock-", suffix=".tmp", dir=directory)
    with os.fdopen(descriptor, "wb") as output:
        for formula in ("cmake", "wget", "openssl@3", "libunistring", "gettext", "libidn2", "libpsl", "json-c", "pkgconf", "ca-certificates"):
            output.write(b"directory\0" + os.fsencode(work / "logs" / formula) + b"\0")
    Path(name).replace(Path(name).with_suffix(".logs"))
program = r'''
set -euo pipefail
work=$1
phase=$2
export HOME="$work/home" HOMEBREW_CACHE="$work/cache" HOMEBREW_LOGS="$work/logs"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_FROM_API=1 HOMEBREW_NO_ANALYTICS=1
export HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_FORCE_VENDOR_RUBY=1 HOMEBREW_MAKE_JOBS=12
export PATH=/usr/local/Homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 DEVELOPER_DIR=/Library/Developer/CommandLineTools
brew=/usr/local/bin/brew
ruby=/usr/local/Homebrew/Library/Homebrew/vendor/portable-ruby/current/bin/ruby
"$brew" developer off
"$brew" config
case "$phase" in
install)
    test ! -e /usr/local/Cellar/cmake
    test ! -e /usr/local/Cellar/wget
    printf 'STOCK_PHASE=cmake_source_install\n'
    "$brew" install --keep-tmp --build-from-source cmake
    printf 'STOCK_PHASE=wget_source_install\n'
    "$brew" install --keep-tmp --build-from-source wget
    "$brew" postinstall ca-certificates
    ;;
reinstall)
    printf 'STOCK_PHASE=cmake_wget_source_reinstall\n'
    "$brew" reinstall --keep-tmp --build-from-source cmake wget
    "$brew" postinstall ca-certificates
    ;;
wget-install)
    test -d /usr/local/Cellar/cmake
    test ! -e /usr/local/Cellar/wget
    printf 'STOCK_PHASE=wget_source_install continuation=1\n'
    "$brew" install --keep-tmp --build-from-source wget
    "$brew" postinstall ca-certificates
    ;;
verify-restored)
    /usr/local/opt/openssl@3/bin/openssl version
    ;;
openssl-probe)
    cd /private/tmp/opensslA3-20260909-1908498-6avyf0/openssl-3.6.4
    set +e
    printf 'PROBE_BEGIN=udp\n'
    /usr/bin/make HARNESS_JOBS=12 test TESTS=test_bio_dgram
    udp_rc=$?
    printf 'PROBE_RESULT=udp rc=%s\n' "$udp_rc"
    printf 'PROBE_BEGIN=storable\n'
    /usr/bin/perl5.18 -MStorable=freeze,thaw -e 'my $a={items=>[1,2,{v=>"roundtrip"}]}; my $b=thaw(freeze($a)); die "bad roundtrip" unless $b->{items}[2]{v} eq "roundtrip"; print "STORABLE_ROUNDTRIP_OK\n";'
    storable_rc=$?
    printf 'PROBE_RESULT=storable rc=%s\n' "$storable_rc"
    printf 'PROBE_BEGIN=fork_fcntl\n'
    /usr/bin/perl5.18 -e 'for (1..8) { my $p=fork(); die "fork: $!" unless defined $p; if (!$p) { require Fcntl; die "bad F_GETFD" unless Fcntl::F_GETFD()>0; exit 0; } waitpid($p,0); die "child status=$?" if $?; } print "FORK_FCNTL_8_OK\n";'
    fcntl_rc=$?
    printf 'PROBE_RESULT=fork_fcntl rc=%s\n' "$fcntl_rc"
    test "$udp_rc:$storable_rc:$fcntl_rc" = 0:0:0
    exit $?
    ;;
openssl-remove-probe)
    "$ruby" -e 'names = Dir.children("/private/var/tmp/stock-large-directory-probe"); puts "DIRECTORY_COUNT=#{names.length} EXPECTED=5000"; abort "DIRECTORY_ENTRIES_OMITTED" unless names.sort == (0...5000).map { |n| "entry_%05d_abcdefghijklmnopqrstuvwxyz" % n }'
    exit 0
    ;;
openssl-ocsp-probe)
    cd /private/tmp/opensslA3-20260910-2887785-s8wkfd/openssl-3.6.4
    printf 'STOCK_PHASE=unchanged_ocsp_recipe\n'
    /usr/bin/make HARNESS_JOBS=12 test TESTS=test_ocsp_cert_chain
    exit 0
    ;;
openssl-tests)
    # Current stock formula runs make test only for build-bottle. Execute that
    # supported brew path unchanged to exercise the historical HARNESS_JOBS=12 workload.
    printf 'STOCK_PHASE=openssl_source_build_and_tests HARNESS_JOBS=12\n'
    "$brew" uninstall --ignore-dependencies openssl@3
    "$brew" install --keep-tmp --build-bottle openssl@3
    "$brew" postinstall openssl@3
    ;;
wget-repeat)
    __WGET_REPEAT_PHASE__
    ;;
*) printf 'invalid phase: %s\n' "$phase" >&2; exit 2 ;;
esac
"$ruby" -rjson -e '
  %w[cmake wget].each do |name|
    receipt = Dir["/usr/local/Cellar/#{name}/*/INSTALL_RECEIPT.json"].sort.last
    abort "missing #{name} source receipt" unless receipt
    data = JSON.parse(File.read(receipt))
    abort "#{name} was poured from a bottle" unless data.fetch("poured_from_bottle") == false
    abort "#{name} was built as a bottle" unless data.fetch("built_as_bottle") == false
    header = File.binread("/usr/local/bin/#{name}", 8).unpack("V2")
    abort "#{name} is not x86_64 Mach-O" unless header == [0xfeedfacf, 0x01000007]
    puts "STOCK_SOURCE_RECEIPT_OK #{name} #{receipt}"
  end
'
/usr/local/bin/wget --version
/usr/local/bin/wget --timeout=60 --tries=1 -O "$work/wget-example.html" https://example.com/
test -s "$work/wget-example.html"
printf 'STOCK_CLT13_REPLAY_OK phase=%s\n' "$phase"
'''
program = program.replace(
    "__WGET_REPEAT_PHASE__", wget_repeat_phase(wget_iterations)
)
print("STOCK_WGET_REPEAT iterations=%s acceptance=%s" % (
    wget_iterations, 1 if wget_acceptance else 0
), flush=True)

result = run_guest_shell_argv(
    launcher, prefix,
    ("/usr/bin/env", "-i", "/bin/bash", "-c", program, "west-stock-replay",
     "/" + work.relative_to(prefix).as_posix(), phase),
    cwd=workspace, env=dict(os.environ), timeout_seconds=11400,
)
raise SystemExit(result.returncode)
