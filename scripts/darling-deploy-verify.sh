#!/bin/sh
# darling-deploy-verify.sh -- install built Darling runtime components into a prefix and VERIFY
# every runtime copy. This is now a thin wrapper: the component -> destination table, the
# install and the sha256 check live in ONE place, west_commands/runtime_artifacts.py, which
# `west darling-build --deploy --deploy-component NAME` also uses.
#
# Usage: darling-deploy-verify.sh --prefix PATH --build DIR [--component NAME]...
#   components: mldr dyld libsystem_kernel libunwind libxpc corefoundation foundation
#               objc-trampolines mobiledevice coresimulator oah darlingserver shellspawn
#               vchroot launchd
#
# Why every copy: a component is loaded from a path that is not the one that was deployed unless
# all copies are replaced (the loader takes dyld from <prefix>/libexec/usr/lib/dyld; guest
# processes launch through mldr; several components exist under usr/... and libexec/darling/usr/...).
# A destination whose parent directory does not exist is skipped and reported, never created.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 -B "$here/../west_commands/runtime_artifacts.py" "$@"
