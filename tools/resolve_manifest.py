from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

HF = "https://huggingface.co"
GH = "https://api.github.com"
RAW_GH = "https://raw.githubusercontent.com"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TBD_SHA = "TBD-release"
HF_FILE_KEYS = ("llm", "llm_balanced", "llm_best", "story_llm", "embed")
HF_FILE_LIST_KEYS = ("tts", "tts_optional")


def hf_tree(repo: str, revision: str, client: httpx.Client) -> dict[str, tuple[str, int]]:
    r = client.get(f"{HF}/api/models/{repo}/tree/{revision}", params={"recursive": "true"})
    r.raise_for_status()
    out: dict[str, tuple[str, int]] = {}
    for item in r.json():
        if item.get("type") == "file" and "lfs" in item:
            out[item["path"]] = (item["lfs"]["oid"], int(item["lfs"]["size"]))
    return out


def github_release_assets(
    owner: str, repo: str, tag: str, client: httpx.Client
) -> dict[str, tuple[str, str]]:
    r = client.get(f"{GH}/repos/{owner}/{repo}/releases/tags/{tag}")
    r.raise_for_status()
    out: dict[str, tuple[str, str]] = {}
    for a in r.json()["assets"]:
        digest = (a.get("digest") or "").removeprefix("sha256:")
        out[a["name"]] = (a["browser_download_url"], digest)
    return out


def github_lfs_sha(url: str, client: httpx.Client) -> str | None:
    """sha256 of a Git-LFS tracked file in a GitHub repo, read from its pointer (no content download)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/raw/(.+)$", url)
    if not m:
        return None
    owner, repo, rest = m.groups()
    r = client.get(f"{RAW_GH}/{owner}/{repo}/{rest}", follow_redirects=True)
    if r.status_code != 200 or len(r.content) > 1024:
        return None
    pm = re.search(r"oid sha256:([0-9a-f]{64})", r.text)
    return pm.group(1) if pm else None


def _fill_hf_file(entry: dict, client: httpx.Client, cache: dict) -> None:
    key = (entry["repo"], entry.get("revision", "main"))
    if key not in cache:
        cache[key] = hf_tree(*key, client)
    sha, size = cache[key][entry["file"]]
    entry["sha256"], entry["bytes"] = sha, size


def fill(manifest: dict, client: httpx.Client) -> dict:
    cache: dict = {}
    for prof in manifest["profiles"].values():
        for k in HF_FILE_KEYS:
            if prof.get(k):
                _fill_hf_file(prof[k], client, cache)
        for k in HF_FILE_LIST_KEYS:
            for entry in prof.get(k, []):
                _fill_hf_file(entry, client, cache)
        wr = prof.get("whisper_repo")
        if wr:
            key = (wr["repo"], wr.get("revision", "main"))
            if key not in cache:
                cache[key] = hf_tree(*key, client)
            wr["files_sha256"] = {p: sha for p, (sha, _) in cache[key].items()}
    for b in manifest.get("bins", {}).values():
        if not b:
            continue
        url = b["url"]
        if "github.com" in url and "/releases/download/" in url:
            _, rest = url.split("github.com/", 1)
            owner, repo, _, _, tag, asset = rest.split("/", 5)
            assets = github_release_assets(owner, repo, tag, client)
            if asset in assets and assets[asset][1]:
                b["sha256"] = assets[asset][1]
        else:
            sha = github_lfs_sha(url, client)
            if sha:
                b["sha256"] = sha
    return manifest


def _head(url: str, client: httpx.Client) -> str | None:
    try:
        r = client.head(url, follow_redirects=True)
        if r.status_code >= 400:
            return f"{url}: HTTP {r.status_code}"
    except httpx.HTTPError as ex:
        return f"{url}: {ex}"
    return None


def _check_sha(where: str, sha: object, problems: list[str]) -> None:
    if not sha:
        problems.append(f"{where}: sha256 missing")
    elif not isinstance(sha, str) or not SHA_RE.match(sha):
        problems.append(f"{where}: sha256 is not 64 lowercase hex characters")


def check(manifest: dict, client: httpx.Client) -> list[str]:
    problems: list[str] = []
    for pname, prof in manifest.get("profiles", {}).items():
        entries: list[tuple[str, dict]] = []
        for k in HF_FILE_KEYS:
            if prof.get(k):
                entries.append((f"profiles.{pname}.{k}", prof[k]))
        for k in HF_FILE_LIST_KEYS:
            for i, e in enumerate(prof.get(k, [])):
                entries.append((f"profiles.{pname}.{k}[{i}]", e))
        for where, e in entries:
            _check_sha(where, e.get("sha256"), problems)
            err = _head(f"{HF}/{e['repo']}/resolve/{e.get('revision', 'main')}/{e['file']}", client)
            if err:
                problems.append(err)
        wr = prof.get("whisper_repo")
        if wr:
            if not wr.get("files_sha256"):
                problems.append(f"profiles.{pname}.whisper_repo: files_sha256 empty")
            for fname, sha in wr.get("files_sha256", {}).items():
                _check_sha(f"profiles.{pname}.whisper_repo.{fname}", sha, problems)
                err = _head(f"{HF}/{wr['repo']}/resolve/{wr.get('revision', 'main')}/{fname}", client)
                if err:
                    problems.append(err)
    for name, b in manifest.get("bins", {}).items():
        if b is None:
            continue
        if not (b.get("optional") and b.get("sha256") == TBD_SHA):  # optional pins may wait
            _check_sha(f"bins.{name}", b.get("sha256"), problems)
        err = _head(b["url"], client)
        if err:
            problems.append(f"bins.{name}: {err}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "manifest.json"))
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    path = Path(a.manifest)
    mf = json.loads(path.read_text(encoding="utf-8"))
    with httpx.Client(timeout=60, headers={"User-Agent": "nightshift-resolve"}) as client:
        if a.fill:
            mf = fill(mf, client)
            path.write_text(json.dumps(mf, indent=2) + "\n", encoding="utf-8")
            print(f"filled {path}")
        if a.check:
            probs = check(mf, client)
            for p in probs:
                print("PROBLEM:", p)
            return 1 if probs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
