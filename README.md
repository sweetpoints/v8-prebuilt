# v8-prebuilt

Build the official V8 stable source with the `source_v8` C ABI bridge and publish
checksummed artifacts for Android, iOS, macOS, Linux and Windows. This repository
produces the bridge library and its provenance together. It does not download a
third party V8 binary and wrap it afterwards.

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

The detector does not change checked-in pins or commit to the repository. The
workflow creates a resolved pin file for that run; every matrix build consumes
the same file through `--pins-file`. The run's resolved file is included in the
release. `GH_TOKEN` or `GITHUB_TOKEN`, when available, is used for release lookup.

## Build locally

The unified producer entry point accepts a selected target and an explicit pin
file. For example, on a compatible macOS host:

```sh
python3 tool/v8/build.py bootstrap --target macos-arm64 \
  --pins-file tool/v8/pins.json
python3 tool/v8/build.py build --target macos-arm64 \
  --pins-file tool/v8/pins.json --jobs 6
```

Use the resolved pin file from stable detection's workflow when reproducing that
release. `--cache-root` chooses the V8/depot_tools checkout cache; `--output-root`
chooses the output base, beneath which the full V8 revision identifies artifacts.
Output includes `manifest.json`, target libraries and recorded build inputs.

macOS targets require the matching native host CPU: Darwin ARM64 for
`macos-arm64` and Darwin x86_64 for `macos-x64`, with minimum macOS 13.0.
Android targets require API 26 or later and build on Linux x86_64. Linux x64 and ARM64 also build on Linux
x86_64, with the pinned official compiler and Debian Bullseye target sysroot;
Linux binaries are checked against the GLIBC 2.31 baseline. Linux ARM64 build
success is separate from native ARM64 execution. Windows uses an installed Visual
Studio toolchain with `DEPOT_TOOLS_WIN_TOOLCHAIN=0`, official clang and a static
CRT; the CI runner version is not a promise of a minimum Windows version.
iOS requires a full Xcode installation on Darwin ARM64, produces static SDK
archives, and disables JIT and WebAssembly. Desktop and Android outputs are
`libsource_v8.so`, `libsource_v8.dylib` or `source_v8.dll` as appropriate.

## Download a released target

Release archives use `v8-<version>-<target>.tar.gz`. Each archive contains its
single-target manifest, resolved `pins.json`, the binary, SDK header and collected
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

Also verify the extracted manifest's full source and bridge identity, selected
target and individual binary hash before loading or linking it. SHA256SUMS
checks integrity against the release metadata; it is not an independent digital
signature. Consumers should pin an exact release and approved artifact digest.
iOS provides static archives and SDK headers rather than a normal dynamic
library; follow the iOS target's linking metadata.

## Source and artifact identity

The build pin fixes official V8 and depot_tools revisions. Dependency sources
and compiler inputs follow that V8 revision's DEPS; each target records the
actual host/toolchain identity and hashes of GN arguments, DEPS, dependency
inventory and build definitions. The bridge source hash and C ABI version are
part of the artifact manifest. A consumer must validate both upstream and bridge
identity, target, minimum OS/API, binary size and SHA-256.

Current build definitions disable Intl and Temporal and embed startup data in the
library. These SDKs are not a promise of every V8 optional feature.

The builder compiles the C ABI bridge within the V8 GN graph. Consumers call the
C interface in `src/source_v8.h`; they do not link an independently compiled C++
wrapper against an arbitrary V8 C++ ABI. Build validation and runtime/source
compatibility validation are separate manifest fields. A successful link is
not a successful device test or evidence that all historical book sources work.

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

The bridge and repository code are covered by the [GPL-3.0 license](LICENSE).
V8 and its third party dependencies retain their own upstream licenses; the
bridge's GPL license does not replace those notices. Build artifacts include
collected upstream license/notice files and their hashes. Notice collection is
not a claim that every redistribution obligation has been independently audited.

Because the packaged library combines the bridge and V8, consumers must account
for the bridge's license as well as V8 and dependency licenses when distributing
it. These artifacts are not offered as a BSD-only V8 distribution. Corresponding
source must be recoverable from the builder tag, source pins and recorded inputs.
