"""
GitHub Sync Helper
==================
Fetches the latest crawl data artifacts or release assets from GitHub Actions.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import httpx

from .logger import get_logger, console

log = get_logger("github_sync")

DEFAULT_REPO = "minhduc1212/hako_downloader"


def detect_repo_from_git() -> str:
    """Tries to detect GitHub 'owner/repo' from local git remote."""
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = res.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+)/([^/\.]+)", url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    except Exception:
        pass
    return DEFAULT_REPO


def get_github_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Builds headers for GitHub API requests."""
    headers = {
        "User-Agent": "Hako-Downloader-Sync/2.0",
        "Accept": "application/vnd.github+json",
    }
    tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def download_latest_github_artifact(
    repo: Optional[str] = None,
    token: Optional[str] = None,
    dest_dir: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Finds and downloads the latest daily crawl artifact or release asset.
    Returns: (downloaded_zip_path, error_message)
    """
    repo_name = repo or detect_repo_from_git()
    dest = Path(dest_dir or tempfile.gettempdir())
    dest.mkdir(parents=True, exist_ok=True)
    headers = get_github_headers(token)

    # 1. Try downloading from Releases first (if rolling release exists, no token needed for public repo)
    try:
        release_url = f"https://api.github.com/repos/{repo_name}/releases"
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            resp = client.get(release_url)
            if resp.status_code == 200:
                releases = resp.json()
                for rel in releases:
                    for asset in rel.get("assets", []):
                        asset_name = asset.get("name", "")
                        if asset_name.endswith(".zip") and ("daily" in asset_name.lower() or "hako" in asset_name.lower()):
                            download_url = asset.get("browser_download_url")
                            if download_url:
                                log.info(f"[GitHubSync] Found release asset: {asset_name}. Downloading...")
                                target_path = dest / asset_name
                                with client.stream("GET", download_url) as r_stream:
                                    r_stream.raise_for_status()
                                    with open(target_path, "wb") as f:
                                        for chunk in r_stream.iter_bytes(chunk_size=1024 * 64):
                                            f.write(chunk)
                                log.info(f"[GitHubSync] Downloaded release asset to {target_path}")
                                return target_path, None
    except Exception as e:
        log.debug(f"[GitHubSync] Release check failed or no release assets: {e}")

    # 2. Try Actions Artifacts API
    try:
        artifacts_url = f"https://api.github.com/repos/{repo_name}/actions/artifacts?per_page=10"
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            resp = client.get(artifacts_url)
            if resp.status_code == 401 or resp.status_code == 403:
                return None, (
                    "GitHub API yêu cầu authentication token để tải artifacts. "
                    "Vui lòng cung cấp token qua --token <TOKEN> hoặc biến môi trường GITHUB_TOKEN, "
                    "hoặc tải file zip trực tiếp từ tab Actions trên GitHub rồi chạy: "
                    "python main.py merge --source <đường_dẫn_file_zip>"
                )
            if resp.status_code != 200:
                return None, f"GitHub API error (status {resp.status_code}): {resp.text}"

            data = resp.json()
            artifacts = data.get("artifacts", [])
            if not artifacts:
                return None, f"Không tìm thấy artifact nào trong repository {repo_name}."

            # Find matching artifact
            target_artifact = None
            for art in artifacts:
                if not art.get("expired", False):
                    name = art.get("name", "").lower()
                    if "daily" in name or "hako" in name:
                        target_artifact = art
                        break
            if not target_artifact:
                target_artifact = artifacts[0]

            art_id = target_artifact["id"]
            art_name = target_artifact["name"]
            download_url = f"https://api.github.com/repos/{repo_name}/actions/artifacts/{art_id}/zip"

            tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not tok:
                return None, (
                    f"Tìm thấy artifact '{art_name}' (ID: {art_id}), nhưng GitHub yêu cầu token xác thực để tải trực tiếp artifact zip. "
                    "Vui lòng cung cấp --token <TOKEN> hoặc tải thủ công file zip từ GitHub Actions tab rồi dùng: "
                    f"python main.py merge --source <đường_dẫn_file_zip>"
                )

            log.info(f"[GitHubSync] Downloading artifact '{art_name}' (ID: {art_id})...")
            target_zip = dest / f"{art_name}.zip"

            with client.stream("GET", download_url, headers=headers) as r_stream:
                if r_stream.status_code in (401, 403):
                    return None, "Token không hợp lệ hoặc không có quyền truy cập artifact (status 401/403)."
                r_stream.raise_for_status()
                with open(target_zip, "wb") as f:
                    for chunk in r_stream.iter_bytes(chunk_size=1024 * 64):
                        f.write(chunk)

            log.info(f"[GitHubSync] Downloaded artifact to {target_zip}")
            return target_zip, None

    except Exception as e:
        return None, f"Lỗi khi tải artifact từ GitHub: {e}"
