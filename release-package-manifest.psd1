@{
    SchemaVersion = 1
    SirilVersion = "1.4.1"

    # Every non-Siril file shipped in both Windows release archives.
    CommonFiles = @(
        ".gitignore"
        ".github\ISSUE_TEMPLATE\bug_report.md"
        ".github\workflows\tests.yml"
        "build-release-packages.ps1"
        "build-seestar-metcalf-stack-exe.ps1"
        "CHANGELOG-en.md"
        "CHANGELOG.md"
        "DEVELOPMENT.md"
        "get-cacert.ps1"
        "get-cacert.sh"
        "LICENSE"
        "macos\build-droplet.sh"
        "macos\SeestarMetcalfStackLauncher.applescript"
        "PUBLISHING.md"
        "README-en.md"
        "README-macOS.md"
        "README-Siril-CLI.md"
        "README.md"
        "release-package-manifest.psd1"
        "requirements.txt"
        "scripts\astrometry_solve.py"
        "scripts\horizons_ephemeris.py"
        "scripts\moving_target_pipeline.py"
        "scripts\moving_target_stack.py"
        "scripts\sharpcap_stacklog.py"
        "seestar-metcalf-stack.cmd"
        "seestar-metcalf-stack.sh"
        "set-astrometry-api-key.cmd"
        "set-astrometry-api-key.sh"
        "setup-macos.sh"
        "setup-python-deps.cmd"
        "SHARPCAP-TIMESTAMPS.md"
        "siril-cli.cmd"
        "tests\test_moving_target_options.py"
        "THIRD-PARTY-NOTICES.md"
        "TROUBLESHOOTING-en.md"
        "TROUBLESHOOTING.md"
        "verify-release-packages.ps1"
    )

    GeneratedFiles = @(
        "seestar-metcalf-stack.exe"
        "cacert.pem"
        "PACKAGE-CONTENTS.sha256"
    )

    SirilOnlyFiles = @(
        "SIRIL-LICENSE-GPLv3.md"
        "SIRIL-SOURCE.txt"
    )

    SirilOnlySourceFiles = @(
        "SIRIL-SOURCE.txt"
    )

    # A pinned Siril distribution is expected, so exact source/target file and
    # byte totals are checked during the build. These lower bounds also make
    # ZIP verification independent from the local source tree.
    SirilRequiredFiles = @(
        "bin\siril-cli.exe"
        "bin\siril.exe"
        "bin\libopenblas.dll"
    )
    SirilMinimumFileCount = 5000
    SirilMinimumBytes = 250MB

    ExecutableMinimumBytes = 10MB
    CaBundleMinimumBytes = 100KB
}
