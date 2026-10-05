# v8-prebuilt

Build the official V8 stable source with its complete stable feature profile
and publish checksummed **pure V8 SDKs** for
Android, iOS, macOS, Linux and Windows. Each target SDK supplies the official V8
monolithic static library, public C++ headers and recorded compiler/linking inputs.
Application bridges, Dart FFI wrappers and book-source APIs belong in their
consumer repositories, including Legado; they are not SDK binaries here.

The initial source pin is V8 **15.4.80.24**, revision
`e422f6ef0c7b877b04e4872fd0bd3a1cc2ec2eee`. V8 and depot_tools come from their
[official V8 repository](https://chromium.googlesource.com/v8/v8.git) and
[official depot_tools repository](https://chromium.googlesource.com/chromium/tools/depot_tools.git).
The release pin file records full revisions; the version label alone is not a
source identity.

## Targets

| Platform | Target | Architecture | Execution policy |
|---|---|---|---|
| Android | `android-arm64` | arm64-v8a | V8 JIT |
| Android | `android-x64` | x86_64 | V8 JIT |
| iOS device | `ios-arm64` | ARM64 | JITless |
| iOS simulator | `ios-simulator-arm64` | ARM64 | JITless |
| macOS | `macos-arm64` | ARM64 | V8 JIT |
| macOS | `macos-x64` | x86_64 | V8 JIT |
| Linux | `linux-x64` | x86_64 | V8 JIT |
| Linux | `linux-arm64` | ARM64 | V8 JIT |
| Windows | `windows-x64` | x86_64 | V8 JIT |
| Windows | `windows-arm64` | ARM64 | V8 JIT |

These are the build matrix targets. A target appearing in this table does not
establish a successful CI build, device execution or consumer acceptance.
There is no published-release or completed-CI claim for this initial checkout.

iOS uses JITless V8 and cannot be treated as equivalent to the JIT targets.
Applications must observe the runtime policy and deployment requirements in each
artifact's manifest. macOS applications adopting Hardened Runtime need the JIT
entitlement when using the JIT build; packaging a library does not configure a
consumer's app signing or entitlements.

## Detect the stable source

```sh
python3 tool/detect_stable.py --repository sweetpoints/v8-prebuilt \
  --output artifacts/stable.json
```

Detection uses ChromiumDash's Linux Stable channel to find the stable milestone,
resolves that milestone's official V8 branch head, and reads `v8-version.h` at
the full resolved revision. It requires a non-candidate version and checks that
the stable milestone did not change during resolution. This follows the stable
branch rather than the highest V8 development tag. Its JSON includes `version`,
`revision`, `branch`, `tag`, `should_build` and release status. An existing complete
release with matching provenance avoids rebuilding; conflicting or incomplete
published provenance fails instead of silently replacing a release.

The [release workflow](.github/workflows/release.yml) schedules detection every
six hours, at minute 17 (UTC). It can also be started manually:

```sh
gh workflow run release.yml --repo sweetpoints/v8-prebuilt
```

All ten builds use the same uploaded `stable-pins.json`. The publication job
depends on the complete build matrix and required native probe jobs. Desktop
probes compile and run a V8 C++ embedder on the matching supported host; Android and iOS build outputs
do not by themselves claim device execution. The workflow is implemented, but
its implementation is not evidence that a remote run has already succeeded.

The detector does not change checked-in pins or commit to the repository. The
workflow creates a resolved pin file for that run; every matrix build consumes
the same file through `--pins-file`. The run's resolved file is included in the
release. HTTP, Git or malformed-metadata failures stop detection with exit code 1;
they do not produce a successful build decision. For Actions, pass
`--github-output "$GITHUB_OUTPUT"` to append version/revision/tag/should_build
outputs. `GH_TOKEN` or `GITHUB_TOKEN`, when available, is used for release lookup.

## Build locally

The unified producer entry point accepts a selected target and an explicit pin
file. For example, on a compatible macOS host:

```sh
python3 tool/v8/build.py bootstrap --target macos-arm64 \
  --pins-file tool/v8/pins.json
python3 tool/v8/build.py build --target macos-arm64 \
  --pins-file tool/v8/pins.json --jobs 6
```

Create a local resolved pin file from the detector's JSON if reproducing the
currently detected stable source:

```sh
python3 tool/release_pins.py --detection artifacts/stable.json \
  --output artifacts/resolved-pins.json
```

Use that file with `--pins-file`, or use the exact resolved `pins.json` downloaded
from the release being reproduced. `--cache-root` chooses the V8/depot_tools checkout cache; `--output-root`
chooses the output base, beneath which the full V8 revision identifies artifacts.
Output includes `manifest.json`, target libraries and recorded build inputs.

macOS targets require the matching native host CPU: Darwin ARM64 for
`macos-arm64` and Darwin x86_64 for `macos-x64`, with minimum macOS 13.0.
Android targets require API 26 or later and build on Linux x86_64. Linux x64 and ARM64 also build on Linux
x86_64, with the pinned official compiler and Debian Bullseye target sysroot;
The SDK records its Bullseye sysroot requirement. Static archives alone do not
establish final GLIBC compatibility; the linked consumer probe is checked
separately. Linux ARM64 build success is separate from native execution. Windows uses an installed Visual
Studio toolchain with `DEPOT_TOOLS_WIN_TOOLCHAIN=0`, official clang and a static
CRT; the CI runner version is not a promise of a minimum Windows version.
iOS requires a full Xcode installation on Darwin ARM64, produces static SDK
archives, and disables JIT and WebAssembly. All targets produce static V8 SDKs. Use their recorded libraries, feature
definitions, C++ runtime dependencies and linker flags when compiling an embedder.

## Link an embedder

Each `v8-static-sdk` target provides `lib/`, `include/` and `linking.json`.
The monolith and C++ runtime archive names follow the collected GN outputs:
Unix targets use `.a` archives and Windows uses `.lib`. Linux, Android, macOS
and Windows include the matching custom libc++/libc++abi runtime archive and
configuration headers. iOS uses Xcode's system libc++ and does not redistribute
that system runtime; required compiler runtime archives are recorded separately
in its SDK library list. Public and generated V8 headers are collected together.

`linking.json` schema version 1 records `includeDirs`, `defines`,
`compileOptions`, `libraries`, `linkOptions` and `systemLibraries`; target
metadata also specifies compiler style, C++ runtime or sysroot requirements.
Paths are relative to the SDK target directory. Compile with all recorded
feature definitions and matching headers, then link the listed archives and
system libraries. Use an appropriate pinned compiler plus the required Android
NDK, Chromium sysroot or Xcode SDK; external sysroots are not copied into the SDK.
A static archive alone cannot prove final Android 16 KiB ELF alignment.

For iOS minimum version 15.0, use the device or simulator target and its recorded
clang target separately. Set `v8::V8::SetFlagsFromString("--jitless")` before V8
initialization. The iOS validation probe compiles and links official V8 APIs;
it does not execute an iOS runtime or establish device acceptance.

The workflow's SDK probes compile/link a C++ embedder against the supplied SDK.
Desktop native execution is separate from cross compilation; the Linux ARM64
runner executes the exact cross-built probe and verifies its associated hashes.
There are no application bridge exports or custom runtime APIs in these SDKs.

## Download a released target

Release archives use `v8-<version>-<target>.tar.gz`. Each archive contains its
single-target manifest, resolved `pins.json`, static libraries, public V8 headers, link metadata and collected
license files. A complete release also includes `release-manifest.json`,
`pins.json` and `SHA256SUMS`. Until a release is published, these filenames are
the packaging contract rather than available downloads.

For example, after the desired release exists:

```sh
version=15.4.80.24
target=android-arm64
gh release download "v8-$version" --repo sweetpoints/v8-prebuilt \
  --pattern "v8-$version-$target.tar.gz" --pattern SHA256SUMS \
  --pattern release-manifest.json --pattern pins.json
python3 - "$version" "$target" <<'PYVERIFY'
import hashlib, pathlib, sys
name = f"v8-{sys.argv[1]}-{sys.argv[2]}.tar.gz"
checks = dict(line.split(None, 1)[::-1] for line in pathlib.Path('SHA256SUMS').read_text().splitlines())
expected = checks[name]
actual = hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit('archive SHA-256 mismatch')
print('archive SHA-256 verified')
PYVERIFY
tar -xzf "v8-$version-$target.tar.gz"
```

Also verify the extracted manifest's full upstream source and SDK producer identity, selected
target and individual library hashes before linking it. SHA256SUMS
checks integrity against the release metadata; it is not an independent digital
signature. Consumers should pin an exact release and approved artifact digest.
Follow the target's linking metadata rather than copying compiler flags from
another architecture or V8 build.

## Source and artifact identity

The build pin fixes official V8 and depot_tools revisions. Dependency sources
and compiler inputs follow that V8 revision's DEPS; each target records the
actual host/toolchain identity and hashes of GN arguments, DEPS, dependency
inventory and build definitions. Producer inputs and SDK file inventories are
part of the artifact manifest. A consumer must validate source and producer
identity, target, minimum OS/API, library sizes and SHA-256.

The stable feature profile enables Intl with embedded ICU data and Temporal on
all ten targets. Android, macOS, Linux and Windows retain JIT and WebAssembly;
iOS device and simulator remain JITless with WebAssembly disabled to respect
the platform execution policy. Startup data is embedded, and no separate ICU
data file is required. The pinned stable V8 runtime defaults apply: Temporal
is a shipped default feature in the resolved 15.4.80.25 source; the consumer
probe uses it without adding a harmony flag. This does not enable every
experimental upstream flag.

The native SDK probe checks Intl NumberFormat and Segmenter, Temporal and,
on supported targets, a WebAssembly result plus Promise handling. Probe code
and enabled build switches describe the verification contract; they do not
claim that a new remote build or target execution has already passed.

Consumers compile against the SDK's matching official V8 C++ headers and feature
definitions, and link the provided monolith with its recorded standard-library
and system dependencies. These inputs are part of the SDK contract; an arbitrary
V8 header/library pair or unrelated libc++ build is not interchangeable.
Build and link probes are distinct from runtime execution. A successful compile
or link is not a successful device test or evidence that all historical book
sources work. Probe source templates may be used for validation; they do not
turn the SDK into an application bridge distribution.

A release tag has the form `v8-<version>` and identifies the **builder repository
commit**. The release also carries the resolved pin file and full upstream
source revision. The updater does not automatically commit a changed pins.json.
All required matrix targets and release verification must pass before a release
is published; a partial matrix is not a complete release.

## Assemble and publish a complete release

After collecting the ten successful target outputs under `artifacts/inputs`,
package them using the exact resolved pin file and builder commit:

```sh
python3 tool/release_package.py --inputs artifacts/inputs \
  --pins-file artifacts/resolved-pins.json --output dist \
  --builder-revision "$(git rev-parse HEAD)"
```

The packager rejects a missing, duplicate or unknown target, provenance conflicts
and mismatched binary/header/license hashes. It generates deterministic tar.gz
archives, the release manifest, resolved pins and checksum list. Publish with
`GH_TOKEN` set in the environment:

```sh
python3 tool/release_publish.py --directory dist \
  --repository sweetpoints/v8-prebuilt
```

The publisher uploads through a draft, reads uploaded assets back and checks them
before making the release public. Existing differing assets are not overwritten;
a matching draft can resume. Publication is an external action, not a side
effect of local compilation. A public release still does not establish every
consumer application's signing, packaging, runtime or source compatibility.

## Licensing

Repository build and release scripts derived from Legado are covered by the
[GPL-3.0 license](LICENSE). The V8 SDK binaries contain upstream V8 and its
recorded dependencies, not Legado's GPL application bridge. V8 and dependency
licenses retain their upstream scope; the repository script license does not
relabel those upstream sources or SDK components.

SDK archives include collected upstream license/notice files and their hashes.
Source provenance is recoverable from the full official revision, DEPS and
recorded toolchain inputs. Consumers distributing an application bridge must
handle that bridge's license in its own repository; this SDK supplies no
`source_v8` or `sv8_*` bridge. Notice collection is not a claim that all
redistribution obligations have been independently audited.
