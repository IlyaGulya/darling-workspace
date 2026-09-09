#!/usr/bin/env python3
"""Exercise CMake installation isolation against the selected product source."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    source_root = Path(os.environ['DARLING_TEST_SOURCE'])
    with tempfile.TemporaryDirectory(prefix='homebrew-stage-isolation-') as directory:
        root = Path(directory)
        source = root / 'source'
        component = source / 'src/external/perl'
        component.mkdir(parents=True)
        build = root / 'build'
        prefix = root / 'real prefix must remain absent'
        (source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.18)\nproject(stage NONE)\nadd_subdirectory(src/external/perl)\n')
        (component / 'payload').write_text('real CMake installation payload\n')
        (component / 'CMakeLists.txt').write_text('''
    install(FILES payload DESTINATION libexec/darling/System/Library/Perl/5.18)
    install(FILES payload DESTINATION libexec/darling/Library/Perl/5.18)
    install(FILES payload DESTINATION libexec/darling/usr/bin)
    install(FILES payload DESTINATION "${CMAKE_INSTALL_PREFIX}/libexec/darling/System/Library/Caches/dsym/files")
    ''' + f'include("{source_root}/cmake/InstallSymlink.cmake")\n' + '''
    InstallSymlink(../files/payload "${CMAKE_INSTALL_PREFIX}/libexec/darling/System/Library/Caches/dsym/uuid/payload")
    ''')
        subprocess.run(['cmake', '-S', str(source), '-B', str(build), '-DCMAKE_INSTALL_PREFIX=' + str(prefix)], cwd=root, check=True)
        base = root / 'base.json'
        base.write_text(json.dumps({'schema': 1, 'entrypoints': [], 'resources': []}))
        output = build / 'manifest.json'
        subprocess.run(['python3', '-B', str(source_root / 'cmake/stage-homebrew-perl.py'),
                        '--cmake', 'cmake', '--build-dir', str(build), '--install-prefix', str(prefix),
                        '--manifest-input', str(base), '--manifest-output', str(output)],
                       env={**os.environ, 'DESTDIR': str(root / 'inherited destination')}, cwd=root, check=True)
        assert not prefix.exists(), 'component staging populated the real prefix'
        assert not (root / 'inherited destination').exists(), 'inherited DESTDIR redirected staging'
        resources = json.loads(output.read_text())['resources']
        assert len(resources) == 3, resources
        for item in resources:
            path = Path(item['host_path'])
            assert path.resolve().is_relative_to(build)
            assert path.read_text() == 'real CMake installation payload\n'
        staged_prefix = build / 'rootless-homebrew-perl' / prefix.relative_to('/')
        alias = staged_prefix / 'libexec/darling/System/Library/Caches/dsym/uuid/payload'
        assert alias.is_symlink() and alias.read_text() == 'real CMake installation payload\n'
        print('PASS real CMake relative install, absolute dSYM install, symlink install and DESTDIR isolation')


if __name__ == "__main__":
    main()
