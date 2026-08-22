@{
    SchemaVersion = 2
    SirilVersion = "1.4.1"

    # Every non-Siril file shipped in both Windows release archives.
    CommonFiles = @(
        "build-seestar-metcalf-stack-exe.ps1"
        "CHANGELOG-en.md"
        "CHANGELOG.md"
        "DEVELOPMENT.md"
        "get-cacert.ps1"
        "get-cacert.sh"
        "LICENSE"
        "macos\build-droplet.sh"
        "macos\SeestarMetcalfStackLauncher.applescript"
        "README-en.md"
        "README-macOS.md"
        "README.md"
        "requirements.txt"
        "scripts\astrometry_solve.py"
        "scripts\fits_preview.py"
        "scripts\horizons_ephemeris.py"
        "scripts\moving_target_pipeline.py"
        "scripts\moving_target_stack.py"
        "scripts\sharpcap_stacklog.py"
        "scripts\siril_preprocessing.py"
        "scripts\sun_pa.py"
        "seestar-metcalf-stack.cmd"
        "seestar-metcalf-stack.sh"
        "set-astrometry-api-key.cmd"
        "set-astrometry-api-key.sh"
        "setup-macos.sh"
        "setup-python-deps.cmd"
        "SHARPCAP-TIMESTAMPS.md"
        "siril-cli.cmd"
        "THIRD-PARTY-NOTICES.md"
        "TROUBLESHOOTING-en.md"
        "TROUBLESHOOTING.md"
    )

    # Development and publishing assets must never leak into end-user ZIPs.
    # Directory entries end with a slash and are checked as path prefixes.
    ForbiddenPackagePaths = @(
        ".github/"
        ".gitignore"
        "developer-tools/"
        "PUBLISHING.md"
        "README-Siril-CLI.md"
        "PLATE-SOLVE-BENCHMARK.md"
        "build-release-packages.ps1"
        "release-package-manifest.psd1"
        "run-plate-solve-benchmark.cmd"
        "run-siril-scale-tolerance.cmd"
        "scripts/plate_solve_benchmark.py"
        "scripts/siril_scale_tolerance.py"
        "tests/"
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
