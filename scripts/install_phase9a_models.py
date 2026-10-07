"""Download only the compact Phase 9A voice models to D:\\FRIDAY.

Existing files are reused only when their byte size and SHA-256 match the
audited asset. Files are never overwritten. Every asset is checked against the
500 MB gate before it is downloaded.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from friday.voice.models import STT_MODEL_DIR, VOICE_MODELS_DIR, WAKE_MODEL_DIR


MAX_MODEL_BYTES = 500 * 1024 * 1024
ASSETS = (
    (STT_MODEL_DIR, "encoder-epoch-99-avg-1.int8.onnx",
     "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17/resolve/main/encoder-epoch-99-avg-1.int8.onnx",
     "3810755ce7c3ab26b42a8bcf39d191308fa27fb0f53358823ba46141d03b7eb3"),
    (STT_MODEL_DIR, "decoder-epoch-99-avg-1.int8.onnx",
     "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17/resolve/main/decoder-epoch-99-avg-1.int8.onnx",
     "21e2a2acd961b3ac72f55be2f10f1a285e1b0b0ba010d7c0b6eab141411b163c"),
    (STT_MODEL_DIR, "joiner-epoch-99-avg-1.int8.onnx",
     "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17/resolve/main/joiner-epoch-99-avg-1.int8.onnx",
     "e085d73b593cf9b0707f370dbd656d58327d3fe36d80d849202ef81df02cb01e"),
    (STT_MODEL_DIR, "tokens.txt",
     "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17/resolve/main/tokens.txt",
     "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb"),
    (VOICE_MODELS_DIR, "en_US-lessac-medium.onnx",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
     "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f"),
    (VOICE_MODELS_DIR, "en_US-lessac-medium.onnx.json",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
     "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0"),
)
WAKE_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)
WAKE_ARCHIVE_PREFIX = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/"
WAKE_FILES = (
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt",
    "bpe.model",
)
WAKE_SHA256 = {
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx": "1e721676515bcd42a186979733981213c66c80db680e1cc582dfedf3be76e678",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx": "e40ff43297abe815e8898494c17e71bba2152d9d40fa3eb803f75d0f7533329a",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx": "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c",
    "tokens.txt": "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53",
    "bpe.model": "c8a2a0129c4ab8e463164c142f82d25649661b122c8cd0b7aab5c9e80b90ad24",
}


def remote_size(url: str) -> int:
    request = Request(url, method="HEAD", headers={"User-Agent": "FRIDAY-Phase9A/1.0"})
    with urlopen(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
    if length is None:
        raise RuntimeError(f"Remote size is unavailable for {url}")
    return int(length)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def approval_required(filename: str, size: int) -> RuntimeError:
    return RuntimeError(
        f"MODEL_APPROVAL_REQUIRED: model={filename}; size_bytes={size}; "
        "purpose=local English STT, wake spotting, or speech output; "
        "alternatives=use push-to-talk without wake or install only STT/TTS assets; "
        "expected_ram=not loaded until first use and measured by phase9a_smoke.py"
    )


def preflight_sizes() -> tuple[dict[tuple[Path, str], int], int | None]:
    sizes: dict[tuple[Path, str], int] = {}
    for directory, filename, url, _sha256 in ASSETS:
        size = remote_size(url)
        if size > MAX_MODEL_BYTES:
            raise approval_required(filename, size)
        sizes[(directory, filename)] = size

    wake_paths = [WAKE_MODEL_DIR / name for name in WAKE_FILES]
    if all(path.is_file() and path.stat().st_size > 0 for path in wake_paths):
        return sizes, None
    wake_archive_size = remote_size(WAKE_ARCHIVE_URL)
    if wake_archive_size > MAX_MODEL_BYTES:
        raise approval_required("sherpa-onnx-kws-zipformer-gigaspeech-3.3M archive", wake_archive_size)
    return sizes, wake_archive_size


def install_asset(
    directory: Path, filename: str, url: str, size: int, expected_sha256: str, *, dry_run: bool,
) -> tuple[str, int, str | None]:
    if size > MAX_MODEL_BYTES:
        raise RuntimeError(f"MODEL_APPROVAL_REQUIRED: {filename} is {size} bytes")
    target = directory / filename
    if target.exists():
        actual = target.stat().st_size
        if actual != size:
            raise RuntimeError(
                f"Existing asset differs in size; left untouched: {target} ({actual} vs {size} bytes)"
            )
        digest = sha256_file(target)
        if digest != expected_sha256:
            raise RuntimeError(f"Existing asset fails SHA-256; left untouched: {target}")
        return "REUSED", actual, digest
    if dry_run:
        return "PLANNED", size, None

    directory.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        request = Request(url, headers={"User-Agent": "FRIDAY-Phase9A/1.0"})
        with urlopen(request, timeout=60) as response, temporary.open("xb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_MODEL_BYTES:
                    raise RuntimeError(f"MODEL_APPROVAL_REQUIRED: {filename} exceeded 500 MB")
                output.write(chunk)
                digest.update(chunk)
        if received != size:
            raise RuntimeError(f"Download size mismatch for {filename}: expected {size}, received {received}")
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"Download SHA-256 mismatch for {filename}")
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return "DOWNLOADED", received, expected_sha256


def install_wake_archive(archive_size: int | None, *, dry_run: bool) -> list[tuple[str, int, str | None]]:
    existing = [WAKE_MODEL_DIR / name for name in WAKE_FILES]
    if all(path.is_file() and path.stat().st_size > 0 for path in existing):
        reused = []
        for name, path in zip(WAKE_FILES, existing):
            digest = sha256_file(path)
            if digest != WAKE_SHA256[name]:
                raise RuntimeError(f"Existing wake asset fails SHA-256; left untouched: {path}")
            reused.append(("REUSED", path.stat().st_size, digest))
        return reused
    if archive_size is None:
        raise RuntimeError("Wake model sizes were not preflighted.")
    if dry_run:
        return [("ARCHIVE_PLANNED", archive_size, None)]

    cache = Path(r"D:\FRIDAY\data\cache\voice")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="sherpa-kws-", suffix=".tar.bz2", dir=cache, delete=False) as tmp:
        archive_path = Path(tmp.name)
    received = 0
    try:
        request = Request(WAKE_ARCHIVE_URL, headers={"User-Agent": "FRIDAY-Phase9A/1.0"})
        with urlopen(request, timeout=60) as response, archive_path.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_MODEL_BYTES:
                    raise RuntimeError("MODEL_APPROVAL_REQUIRED: wake model archive exceeded 500 MB")
                output.write(chunk)
        if received != archive_size:
            raise RuntimeError(f"Wake archive size mismatch: expected {archive_size}, received {received}")

        directory = WAKE_MODEL_DIR
        directory.mkdir(parents=True, exist_ok=True)
        installed: dict[str, tuple[str, int, str | None]] = {}
        with tarfile.open(archive_path, mode="r:bz2") as archive:
            for member in archive.getmembers():
                name = member.name.removeprefix(WAKE_ARCHIVE_PREFIX)
                if name not in WAKE_FILES or not member.isfile():
                    continue
                target = directory / name
                if target.exists():
                    if target.stat().st_size != member.size or sha256_file(target) != WAKE_SHA256[name]:
                        raise RuntimeError(f"Existing wake asset differs from audited asset; left untouched: {target}")
                    installed[name] = ("REUSED", member.size, WAKE_SHA256[name])
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not read wake model asset: {name}")
                temporary = target.with_name(target.name + ".part")
                digest = hashlib.sha256()
                copied = 0
                try:
                    with source, temporary.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            copied += len(chunk)
                            output.write(chunk)
                            digest.update(chunk)
                    if copied != member.size:
                        raise RuntimeError(f"Wake asset size mismatch for {name}")
                    if digest.hexdigest() != WAKE_SHA256[name]:
                        raise RuntimeError(f"Wake asset SHA-256 mismatch for {name}")
                    temporary.replace(target)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                installed[name] = ("DOWNLOADED", copied, digest.hexdigest())
        missing = set(WAKE_FILES) - set(installed)
        if missing:
            raise RuntimeError(f"Wake archive is missing required assets: {', '.join(sorted(missing))}")
        print(f"WAKE_ARCHIVE_BYTES\t{received}")
        return [installed[name] for name in WAKE_FILES]
    finally:
        archive_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report remote byte sizes without downloading.")
    args = parser.parse_args()
    total = 0
    try:
        sizes, wake_archive_size = preflight_sizes()
        for directory, filename, url, expected_sha256 in ASSETS:
            size = sizes[(directory, filename)]
            status, size, sha = install_asset(
                directory, filename, url, size, expected_sha256, dry_run=args.dry_run,
            )
            total += size
            print(f"{status}\t{size}\t{directory / filename}" + (f"\tsha256={sha}" if sha else ""))
        for name, (status, size, sha) in zip(
            WAKE_FILES, install_wake_archive(wake_archive_size, dry_run=args.dry_run),
        ):
            total += size
            label = ("wake archive" if status == "ARCHIVE_PLANNED" else str(WAKE_MODEL_DIR / name))
            print(f"{status}\t{size}\t{label}" + (f"\tsha256={sha}" if sha else ""))
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
        print(f"MODEL_INSTALL_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"TOTAL_BYTES\t{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
