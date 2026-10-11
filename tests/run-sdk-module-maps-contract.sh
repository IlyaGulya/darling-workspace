#!/usr/bin/env bash
# Contract: Darling's SDK module maps are generated from the headers the tree has.
#
# tools/generate-sdk-modulemap.py filters tools/sdk-module-maps/*.modulemap against
# the SDK's usr/include and writes the maps next to those headers. The generator is
# the only thing allowed to name a header, so this contract holds it to the header
# set: every header the emitted maps name exists, no module survives without a
# header, umbrella or child, a module whose headers are all absent is not emitted,
# a missing SDK tree fails loudly by path, and the maps committed in the SDK are
# exactly what the generator produces from the headers now.
#
# Uncached on purpose: every input lives in the darling source forest, which the
# tier's cache key does not identify - a cached PASS here would be a binding nothing
# re-measured.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
darling="${DARLING_SOURCE:-$repo/../darling}"
if [ ! -d "$darling" ]; then
	printf 'cannot find the darling checkout at %s (set DARLING_SOURCE)\n' "$darling" >&2
	exit 1
fi

tool="$darling/tools/generate-sdk-modulemap.py"
templates="$darling/tools/sdk-module-maps"
sdk_include="$darling/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/usr/include"
for path in "$tool" "$templates" "$sdk_include"; do
	if [ ! -e "$path" ]; then
		printf 'missing %s\n' "$path" >&2
		exit 1
	fi
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1. Generate into a temp dir and check the emitted maps against the header tree.
python3 -B "$tool" --sdk-include "$sdk_include" --template-dir "$templates" \
	--output-dir "$tmp/out" >"$tmp/generate.out"
grep -q "emitted" "$tmp/generate.out"

# 2. The committed SDK maps must be exactly what the generator produces.
#
# The generator is deterministic and its output is what SDK generation commits.
# A difference means either the maps were hand-edited or the headers moved without
# the maps being regenerated.
generated="$(cd "$tmp/out" && find . -maxdepth 1 -name '*.modulemap' | LC_ALL=C sort)"
committed="$(cd "$sdk_include" && find . -maxdepth 1 -name '*.modulemap' | LC_ALL=C sort)"
if [ "$generated" != "$committed" ]; then
	printf 'the SDK does not carry the generated map set\n--- generated\n%s\n--- committed\n%s\n' \
		"$generated" "$committed" >&2
	exit 1
fi
while IFS= read -r map; do
	if ! cmp -s "$tmp/out/$map" "$sdk_include/$map"; then
		printf '%s differs from the generated map; regenerate the SDK\n' "$sdk_include/$map" >&2
		diff -u "$sdk_include/$map" "$tmp/out/$map" >&2 || true
		exit 1
	fi
done <<EOF
$generated
EOF

# 3. Checks that need the module map grammar, on the generated tree.
python3 -B - "$tool" "$templates" "$sdk_include" "$tmp/out" "$tmp/synthetic" <<'PY'
import importlib.util
import os
import sys

tool_path, templates, sdk_include, out_dir, synthetic = sys.argv[1:6]

spec = importlib.util.spec_from_file_location("generate_sdk_modulemap", tool_path)
tool = importlib.util.module_from_spec(spec)
sys.modules["generate_sdk_modulemap"] = tool
spec.loader.exec_module(tool)


def walk(members):
    """Yield every header, umbrella and module in a parsed map."""
    for member in members:
        if isinstance(member, tool.Module):
            yield member
            yield from walk(member.members)
        else:
            yield member


def main():
    # Every header an emitted map names must exist in the header tree.
    named = 0
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".modulemap"):
            continue
        with open(os.path.join(out_dir, name)) as handle:
            members = tool.Parser(tool.lex(handle.read(), name), name).parse()
        for member in walk(members):
            if isinstance(member, tool.Header):
                named += 1
                if not os.path.isfile(os.path.join(sdk_include, member.path)):
                    print("%s names missing header %s" % (name, member.path), file=sys.stderr)
                    return 1
                if member.path in tool.NON_MODULAR_HEADERS:
                    print("%s names a header the tree cannot make a module header: %s"
                          % (name, member.path), file=sys.stderr)
                    return 1
            elif isinstance(member, tool.Umbrella):
                if member.directory and not os.path.isdir(
                        os.path.join(sdk_include, member.directory)):
                    print("%s has an umbrella over a missing directory %s"
                          % (name, member.directory), file=sys.stderr)
                    return 1
    if named == 0:
        print("no emitted map names a header at all", file=sys.stderr)
        return 1

    # No module may survive with no header, umbrella, extern or child, and a
    # `module *` (which has no members of its own: Clang infers one submodule per
    # header from its umbrella) may only appear where its umbrella is.
    def check_modules(members, name, where):
        has_umbrella = any(isinstance(m, tool.Umbrella) for m in members)
        for member in members:
            if not isinstance(member, tool.Module):
                continue
            if member.name == "*":
                if not has_umbrella:
                    print("%s emits module * with no umbrella in %s" % (name, where),
                          file=sys.stderr)
                    return 1
                continue
            if not any(isinstance(m, (tool.Header, tool.Umbrella, tool.Module,
                                     tool.ExternModule))
                       for m in member.members):
                print("%s emits empty module %s" % (name, member.name), file=sys.stderr)
                return 1
            if check_modules(member.members, name, where + "." + member.name):
                return 1
        return 0

    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".modulemap"):
            continue
        with open(os.path.join(out_dir, name)) as handle:
            members = tool.Parser(tool.lex(handle.read(), name), name).parse()
        if check_modules(members, name, ""):
            return 1

    # math.h is claimed inside Darwin: the Swift Darwin overlay's tgmath.swift only
    # sees the C math surface through module Darwin (measured: without this,
    # "cannot find 'acosf' in scope").
    darwin = os.path.join(out_dir, "Darwin.modulemap")
    with open(darwin) as handle:
        if 'header "math.h"' not in handle.read():
            print("Darwin.modulemap does not claim math.h", file=sys.stderr)
            return 1

    # Synthetic tree: a module whose only header is absent must not be emitted, and
    # neither must an umbrella over a missing directory, nor the module * that would
    # then have no umbrella.
    include = os.path.join(synthetic, "include")
    tpl = os.path.join(synthetic, "templates")
    os.makedirs(include)
    os.makedirs(tpl)
    with open(os.path.join(include, "present.h"), "w") as handle:
        handle.write("int present;\n")
    with open(os.path.join(tpl, "module.modulemap"), "w") as handle:
        handle.write(
            'module Present [system] { header "present.h" export * }\n'
            'module Absent [system] { header "absent.h" export * }\n'
            'module Parent [system] { module child { header "absent.h" export * } }\n'
            'module Umbrella [system] { umbrella "absent_dir" module * { export * } }\n'
            'module Star [system] { module * { export * } }\n'
        )
    synthetic_out = os.path.join(synthetic, "out")
    tool.run(tpl, include, synthetic_out, tool.KNOWN_FEATURES, tool.NON_MODULAR_HEADERS, False)
    with open(os.path.join(synthetic_out, "module.modulemap")) as handle:
        emitted = handle.read()
    for expected in ("module Present",):
        if expected not in emitted:
            print("synthetic: %s was not emitted:\n%s" % (expected, emitted), file=sys.stderr)
            return 1
    for absent in ("Absent", "Parent", "Umbrella", "Star"):
        if absent in emitted:
            print("synthetic: %s survived with no satisfiable header:\n%s" % (absent, emitted),
                  file=sys.stderr)
            return 1

    # A missing SDK header tree must fail loudly, naming the path.
    try:
        tool.run(templates, os.path.join(synthetic, "no_such_include"), synthetic_out,
                  tool.KNOWN_FEATURES, tool.NON_MODULAR_HEADERS, False)
    except tool.ModuleMapError as error:
        if "no_such_include" not in str(error):
            print("missing-SDK error does not name the path: %s" % error, file=sys.stderr)
            return 1
    else:
        print("a missing SDK include tree was accepted", file=sys.stderr)
        return 1
    return 0


sys.exit(main())
PY

# 4. The SDK's sys/cdefs.h is emitted from a tracked template, not hand-written
# into generated state.  (It carries the platform selection Apple's SDK resolves
# when it processes the header, which a module map cannot express.)
template_header="$templates/include/sys/cdefs.h"
sdk_header="$sdk_include/sys/cdefs.h"
if [ ! -f "$template_header" ]; then
	printf 'missing tracked header template %s\n' "$template_header" >&2
	exit 1
fi
if [ ! -f "$sdk_header" ] || ! cmp -s "$template_header" "$sdk_header"; then
	printf '%s is not the emitted template %s; regenerate the SDK\n' "$sdk_header" "$template_header" >&2
	exit 1
fi
if cmp -s "$template_header" "$tmp/out/sys/cdefs.h"; then
	: # the scratch generation above wrote it too
else
	printf 'the generator did not emit sys/cdefs.h into %s\n' "$tmp/out" >&2
	exit 1
fi

# 5. Every library link in the SDK is produced by Developer/symlinks.sh, so none
# is hand-made generated state.  A line's created path is its destination when
# that is a file, or the source's basename when the destination is the
# directory $LIBS/ or $LIBS/system.
python3 -B - "$darling" <<'PY2'
import os
import re
import sys

darling = sys.argv[1]
script = os.path.join(darling, "Developer", "symlinks.sh")
sdk_lib = os.path.join(
    darling, "Developer", "Platforms", "MacOSX.platform", "Developer",
    "SDKs", "MacOSX.sdk", "usr", "lib",
)

with open(script) as handle:
    lines = [l.strip() for l in handle if l.strip().startswith("ln ")]

generated = set()
for line in lines:
    match = re.match(r'ln -sfr? "([^"]+)" "([^"]+)"', line)
    if not match:
        print("unparsed ln line: %s" % line, file=sys.stderr)
        sys.exit(1)
    source, destination = match.group(1), match.group(2)
    if "$LIBS" not in destination:
        continue
    if destination.rstrip("/") in ("$LIBS", "$LIBS/system"):
        # A directory destination: ln creates a link named after the source.
        generated.add(os.path.basename(source))
    else:
        generated.add(destination.rsplit("/", 1)[-1])

present = set()
for base, _dirs, files in os.walk(sdk_lib):
    for name in files + [d for d in _dirs if os.path.islink(os.path.join(base, d))]:
        path = os.path.join(base, name)
        if os.path.islink(path):
            present.add(name)

missing = sorted(present - generated)
if missing:
    print("library links no generator produces: %s" % ", ".join(missing), file=sys.stderr)
    sys.exit(1)
if not present:
    print("no library links found under %s" % sdk_lib, file=sys.stderr)
    sys.exit(1)
PY2

if find "$darling/tools" -type d -name __pycache__ -print | grep -q .; then
	find "$darling/tools" -type d -name __pycache__ -print >&2
	exit 1
fi

printf 'PASS sdk-module-maps-contract\n'
