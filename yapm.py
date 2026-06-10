#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
import subprocess
import gzip
import tarfile
import io
from pathlib import Path
from typing import List, Dict, Optional

# ============================================================
# CONFIGURATION PATHS (Defaults)
# ============================================================

APP_VERSION = "0.2-alpha"
CURRENT_VERSION = 1  # Config version

HOME = Path.home()

# These will be dynamically set in configure_paths()
CONFIG_DIR = HOME / ".config" / "yapm"
CONFIG_FILE = CONFIG_DIR / "config.json"

DATA_DIR = HOME / ".local" / "share" / "yapm"
INSTALL_DIR = DATA_DIR / "packages"
DB_FILE = DATA_DIR / "installed.json"

CACHE_DIR = DATA_DIR / "cache"
INDEX_FILE = CACHE_DIR / "index.json"
BIN_DIR = HOME / ".local" / "bin"

DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "mirrors": [
        {"url": "https://archive.ubuntu.com/ubuntu/", "priority": 10},
        {"url": "https://deb.debian.org/debian/", "priority": 20},
        {"url": "https://mirror.rackspace.com/archlinux/", "priority": 30},
        {"url": "https://mirrors.fedoraproject.org/", "priority": 40},
        {"url": "https://yapm.pages.dev/", "priority": 50}
    ]
}

def configure_paths(is_system: bool):
    global CONFIG_DIR, CONFIG_FILE, DATA_DIR, INSTALL_DIR, DB_FILE, CACHE_DIR, INDEX_FILE, BIN_DIR
    if is_system:
        if os.getuid() != 0:
            print("Error: System-wide operations (-S) require sudo privileges.")
            sys.exit(1)
        CONFIG_DIR = Path("/etc/yapm")
        DATA_DIR = Path("/var/lib/yapm")
        BIN_DIR = Path("/usr/local/bin")
    else:
        HOME = Path.home()
        CONFIG_DIR = HOME / ".config" / "yapm"
        DATA_DIR = HOME / ".local" / "share" / "yapm"
        BIN_DIR = HOME / ".local" / "bin"

    CONFIG_FILE = CONFIG_DIR / "config.json"
    INSTALL_DIR = DATA_DIR / "packages"
    DB_FILE = DATA_DIR / "installed.json"
    CACHE_DIR = DATA_DIR / "cache"
    INDEX_FILE = CACHE_DIR / "index.json"

# ============================================================
# INITIALIZATION
# ============================================================

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
    else:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        if config.get("version", 0) < CURRENT_VERSION:
            config["version"] = CURRENT_VERSION
            if "mirrors" not in config:
                config["mirrors"] = DEFAULT_CONFIG["mirrors"]
            save_config(config)

    if not DB_FILE.exists():
        save_db({})

def load_config() -> Dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(config: Dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def load_db() -> Dict:
    with open(DB_FILE) as f:
        return json.load(f)

def save_db(db: Dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=4)

# ============================================================
# UTILITIES
# ============================================================

def normalize(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def sorted_mirrors() -> List[Dict]:
    config = load_config()
    return sorted(config["mirrors"], key=lambda x: x["priority"])

def validate_mirror(url: str) -> bool:
    try:
        if url.startswith("file://"):
            return Path(url[7:]).exists()
        req = urllib.request.Request(normalize(url), method="HEAD", headers={'User-Agent': 'yapm/1.0'})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status < 400
    except Exception:
        return False

def download(url: str, desc: str = "Downloading") -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'yapm/1.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            size = int(response.headers.get('content-length', 0))
            data = b""
            chunk_size = 8192
            downloaded = 0
            while True:
                chunk = response.read(chunk_size)
                if not chunk: break
                data += chunk
                downloaded += len(chunk)
                if size:
                    percent = int(downloaded * 100 / size)
                    cols, _ = shutil.get_terminal_size((80, 20))
                    bar_len = min(40, cols - len(desc) - 20)
                    filled = int(bar_len * downloaded / size)
                    bar = "█" * filled + "-" * (bar_len - filled)
                    print(f"\r{desc}: [{bar}] {percent}% ({downloaded}/{size} bytes)", end="", flush=True)
            print()
            return data
    except Exception as e:
        print(f"\nError downloading {url}: {e}")
        return None

def safe_extract(zip_path: Path, target: Path):
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            member_path = (target / member.filename).resolve()
            if not str(member_path).startswith(str(target.resolve())):
                raise Exception("Unsafe zip detected")
            z.extract(member, target)
            attr = member.external_attr >> 16
            if attr != 0:
                os.chmod(member_path, attr)

def parse_yapm_data(content: str) -> dict:
    data = {"METADATA": {}, "CONTENT": {}, "FILES": {}}
    current_section = None
    
    import re
    # Strip multi-line comments /* ... */
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
            
        # Strip inline comments
        if '//' in line:
            line = line.split('//')[0].strip()
            
        if line.startswith('[') and line.endswith(']'):
            current_section = line[1:-1]
            continue
            
        if current_section and '=' in line:
            parts = line.split('=', 1)
            key = parts[0].strip().strip('"').strip("'")
            val = parts[1].strip()
            
            if val.startswith('[') and val.endswith(']'):
                import ast
                try:
                    val = ast.literal_eval(val)
                except Exception:
                    val = []
            else:
                val = val.strip('"').strip("'")
                
            data[current_section][key] = val
            
    return data

# ============================================================
# MIRROR COMMANDS
# ============================================================

def mirror_list():
    for i, m in enumerate(sorted_mirrors(), 1):
        print(f"[{i}] {m['url']} (priority {m['priority']})")

def mirror_add(url: str, priority: int):
    config = load_config()
    url = normalize(url)
    for m in config["mirrors"]:
        if m["url"] == url:
            print("Mirror already exists.")
            return
    config["mirrors"].append({"url": url, "priority": priority})
    save_config(config)
    print(f"Added mirror {url} with priority {priority}")

def mirror_remove(url: str):
    config = load_config()
    url = normalize(url)
    before = len(config["mirrors"])
    config["mirrors"] = [m for m in config["mirrors"] if m["url"] != url]
    if len(config["mirrors"]) == before:
        print("Mirror not found.")
    else:
        save_config(config)
        print("Mirror removed.")

def mirror_refresh():
    config = load_config()
    valid = []
    print("Refreshing mirrors...")
    for m in config["mirrors"]:
        ok = validate_mirror(m["url"])
        print(f"  {m['url']} -> {'OK' if ok else 'FAILED'}")
        if ok: valid.append(m)
    config["mirrors"] = valid
    save_config(config)
    print("Refresh complete.")

def mirror_preset():
    save_config(DEFAULT_CONFIG)
    print("Restored default mirrors.")

# ============================================================
# PACKAGE EXTRACTION ENGINES
# ============================================================

def extract_deb(data: bytes, target: Path):
    with tempfile.TemporaryDirectory() as td:
        deb_path = Path(td) / "pkg.deb"
        with open(deb_path, "wb") as f:
            f.write(data)
        try:
            print("  Extracting DEB container...")
            subprocess.run(["ar", "x", "pkg.deb"], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for f in Path(td).iterdir():
                if f.name.startswith("data.tar"):
                    print("  Extracting DEB data payload...")
                    subprocess.run(["tar", "-xf", f.name, "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
        except Exception as e:
            print(f"Error extracting DEB package: {e}")
            raise

def extract_arch(data: bytes, target: Path):
    with tempfile.TemporaryDirectory() as td:
        arch_path = Path(td) / "pkg.tar.zst"
        with open(arch_path, "wb") as f:
            f.write(data)
        try:
            print("  Extracting Arch ZSTD container...")
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", "pkg.tar.zst", "-C", str(target)], cwd=td, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"Error extracting Arch package: {e}")
            raise

# ============================================================
# PACKAGE LOGIC
# ============================================================

def parse_debian_index(mirror_url: str, merged_index: dict):
    dist = "jammy" if "ubuntu" in mirror_url else "bookworm"
    url = normalize(mirror_url) + f"dists/{dist}/main/binary-amd64/Packages.gz"
    data = download(url, desc=f"Fetching Debian index from {mirror_url}")
    if not data: return
    
    try:
        print("  Parsing Debian Packages.gz...")
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            content = gz.read().decode('utf-8', errors='ignore')
            
        current_pkg = {}
        for line in content.splitlines():
            if not line.strip():
                if current_pkg and "name" in current_pkg:
                    name = current_pkg["name"]
                    if name not in merged_index["packages"]:
                        merged_index["packages"][name] = {
                            "version": current_pkg.get("version", "0.0.0"),
                            "mirror": mirror_url,
                            "format": "deb",
                            "download_path": current_pkg.get("filename", "")
                        }
                current_pkg = {}
                continue
                
            if line.startswith("Package: "): current_pkg["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Version: "): current_pkg["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("Filename: "): current_pkg["filename"] = line.split(":", 1)[1].strip()
    except Exception as e:
        print(f"Error parsing Debian index: {e}")

def parse_arch_index(mirror_url: str, merged_index: dict):
    url = normalize(mirror_url) + "core/os/x86_64/core.db"
    data = download(url, desc=f"Fetching Arch index from {mirror_url}")
    if not data: return
    
    try:
        print("  Parsing Arch core.db...")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("desc"):
                    f = tar.extractfile(member)
                    if f:
                        content = f.read().decode('utf-8', errors='ignore')
                        lines = content.splitlines()
                        name, version = "", ""
                        for i, line in enumerate(lines):
                            if line == "%NAME%": name = lines[i+1]
                            elif line == "%VERSION%": version = lines[i+1]
                        
                        if name and name not in merged_index["packages"]:
                            merged_index["packages"][name] = {
                                "version": version,
                                "mirror": mirror_url,
                                "format": "arch",
                                "download_path": f"core/os/x86_64/{name}-{version}-x86_64.pkg.tar.zst"
                            }
    except Exception as e:
        print(f"Error parsing Arch index: {e}")

def update_index():
    print("Updating package index...")
    merged_index = {"packages": {}}
    for mirror in sorted_mirrors():
        url = mirror["url"]
        if "ubuntu.com" in url or "debian.org" in url:
            parse_debian_index(url, merged_index)
        elif "archlinux" in url:
            parse_arch_index(url, merged_index)
        else:
            index_url = normalize(url) + "index.json"
            data = download(index_url, desc=f"Fetching YAPM index from {url}")
            if data:
                try:
                    index = json.loads(data)
                    pkgs = index.get("packages", {})
                    if isinstance(pkgs, list):
                        new_pkgs = {p: {"version": "0.0.0", "dependencies": []} for p in pkgs}
                        pkgs = new_pkgs
                        
                    for pkg_name, pkg_info in pkgs.items():
                        if pkg_name not in merged_index["packages"]:
                            pkg_info["mirror"] = url
                            pkg_info["format"] = "yapm"
                            merged_index["packages"][pkg_name] = pkg_info
                except Exception as e:
                    print(f"Error parsing index from {url}: {e}")
                
    with open(INDEX_FILE, "w") as f:
        json.dump(merged_index, f, indent=4)
    print("Index updated.")

def load_index() -> Dict:
    if not INDEX_FILE.exists():
        print("Warning: Local index not found. Run 'yapm update' first.")
        return {"packages": {}}
    with open(INDEX_FILE) as f:
        return json.load(f)

def fetch_package(pkg: str) -> Optional[bytes]:
    idx = load_index()
    pkg_info = idx.get("packages", {}).get(pkg)

    def _try_yapm_urls(mirror_url: str, version: str = "") -> Optional[bytes]:
        """Try bare name then versioned name for YAPM packages."""
        candidates = [f"{pkg}.yapm"]
        if version and version != "0.0.0":
            candidates.append(f"{pkg}-{version}.yapm")
        for candidate in candidates:
            url = normalize(mirror_url) + candidate
            data = download(url, desc=f"Downloading {pkg}")
            if data:
                return data
        return None

    if pkg_info and "mirror" in pkg_info:
        if pkg_info.get("format") in ["deb", "arch"]:
            download_path = pkg_info.get("download_path", "")
            if download_path:
                url = normalize(pkg_info["mirror"]) + download_path
                data = download(url, desc=f"Downloading {pkg}")
                if data: return data
            return None
        else:
            version = pkg_info.get("version", "")
            data = _try_yapm_urls(pkg_info["mirror"], version)
            if data: return data

    for mirror in sorted_mirrors():
        data = _try_yapm_urls(mirror["url"])
        if data:
            return data
    return None

def resolve_dependencies(pkg: str, idx: Dict, db: Dict, to_install: List[str], path: set):
    if pkg in to_install or pkg in db:
        return
    if pkg in path:
        print(f"Error: Circular dependency detected: {' -> '.join(path)} -> {pkg}")
        sys.exit(1)
        
    path.add(pkg)
    pkg_info = idx.get("packages", {}).get(pkg)
    if pkg_info:
        for dep in pkg_info.get("dependencies", []):
            resolve_dependencies(dep, idx, db, to_install, path)
    else:
        print(f"Warning: Package '{pkg}' not found in index. Cannot resolve its dependencies.")
            
    to_install.append(pkg)
    path.remove(pkg)

def _install_single(pkg_name: str, db: Dict, data: bytes, fmt: str):
    target = INSTALL_DIR / pkg_name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    try:
        if fmt == "yapm":
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(data)
            tmp.close()
            safe_extract(Path(tmp.name), target)
            os.unlink(tmp.name)
        elif fmt == "deb":
            extract_deb(data, target)
        elif fmt == "arch":
            extract_arch(data, target)
    except Exception as e:
        print(f"Installation failed: {e}")
        sys.exit(1)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    pkg_meta = {"version": "0.0.0", "dependencies": [], "format": fmt}

    yapm_data_path = target / "yapm.data"
    if yapm_data_path.exists():
        with open(yapm_data_path) as f:
            y_data = parse_yapm_data(f.read())
            
        # Metadata
        meta = y_data.get("METADATA", {})
        pkg_meta["version"] = meta.get("version", "0.0.0")
        if "description" in meta: pkg_meta["description"] = meta["description"]
        if "dependencies" in meta: pkg_meta["dependencies"] = meta["dependencies"]
        
        content_info = y_data.get("CONTENT", {})
        
        # BuildFile
        build_file = content_info.get("BuildFile")
        if build_file and (target / build_file).exists():
            print(f"  Running build script: {build_file}...")
            os.chmod(target / build_file, 0o755)
            subprocess.run([str(target / build_file)], cwd=target, check=True)
            
        # PreInstall
        pre_install = content_info.get("PreInstall")
        if pre_install and (target / pre_install).exists():
            print("  Running pre-install script...")
            os.chmod(target / pre_install, 0o755)
            subprocess.run([str(target / pre_install)], cwd=target, check=True)
            
        # File Mappings
        files_info = y_data.get("FILES", {})
        for src, dest in files_info.items():
            src_path = target / src
            dest_path = Path(dest)
            if src_path.exists():
                print(f"  Mapping file: {src} -> {dest}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if dest_path.exists() or dest_path.is_symlink():
                    os.unlink(dest_path)
                shutil.copy2(src_path, dest_path)
                
        # RunFile
        run_file = content_info.get("RunFile")
        if run_file and (target / run_file).exists():
            dest = BIN_DIR / Path(run_file).name
            if dest.exists() or dest.is_symlink():
                os.unlink(dest)
            os.chmod(target / run_file, 0o755)
            os.symlink(target / run_file, dest)
            print(f"  Linked executable {Path(run_file).name} -> {dest}")
            
        # PostInstall
        post_install = content_info.get("PostInstall")
        if post_install and (target / post_install).exists():
            print("  Running post-install script...")
            os.chmod(target / post_install, 0o755)
            subprocess.run([str(target / post_install)], cwd=target, check=True)
    else:
        # Fallback to simple extraction linking
        bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
        for src_dir in bin_source_dirs:
            if src_dir.exists() and src_dir.is_dir():
                for item in src_dir.iterdir():
                    if item.is_file() and os.access(item, os.X_OK):
                        dest = BIN_DIR / item.name
                        if dest.exists() or dest.is_symlink():
                            os.unlink(dest)
                        os.symlink(item, dest)
                        print(f"  Linked {item.name} -> {dest}")

        metadata_path = target / "metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    pkg_meta.update(json.load(f))
            except Exception:
                pass

    db[pkg_name] = {
        "version": pkg_meta.get("version", "0.0.0"),
        "path": str(target),
        "dependencies": pkg_meta.get("dependencies", []),
        "format": fmt,
        "metadata": pkg_meta
    }

    save_db(db)

def install_package(pkg: str, fmt: str):
    db = load_db()
    idx = load_index()
    pkg_name = pkg
    
    pkg_path = Path(pkg)
    if pkg_path.is_file():
        # Auto-detect format from extension if installing from a local file
        if pkg_path.suffix == ".deb": fmt = "deb"
        elif pkg_path.name.endswith(".pkg.tar.zst"): fmt = "arch"
        elif pkg_path.suffix == ".yapm": fmt = "yapm"
        
        # Determine package name from file name
        if pkg_path.name.endswith(".pkg.tar.zst"):
            pkg_name = pkg_path.name[:-12]
        else:
            pkg_name = pkg_path.stem

        print(f"Installing {fmt.upper()} from local file: {pkg_path}")
        with open(pkg_path, "rb") as f:
            data = f.read()
        _install_single(pkg_name, db, data, fmt)
        print(f"Installed {pkg_name} successfully.")
        return

    # Remote installations currently default to YAPM format unless we add format hints to index
    if pkg_name in db:
        print(f"{pkg_name} is already installed.")
        return

    to_install = []
    resolve_dependencies(pkg_name, idx, db, to_install, set())
    
    if not to_install:
        print("Nothing to install.")
        return
        
    print(f"The following packages will be installed: {', '.join(to_install)}")
    
    for p in to_install:
        print(f"Installing {p}...")
        data = fetch_package(p)
        if not data:
            print(f"Failed to fetch {p}. Aborting.")
            sys.exit(1)
        
        # Determine format from index
        pkg_info = idx.get("packages", {}).get(p, {})
        fetched_fmt = pkg_info.get("format", "yapm")
        _install_single(p, db, data, fetched_fmt)
        print(f"Installed {p}.")

def remove_package(pkg: str):
    db = load_db()
    if pkg not in db:
        print(f"Package '{pkg}' not installed.")
        return

    target = Path(db[pkg]["path"])
    bin_source_dirs = [target / "src", target / "usr" / "bin", target / "bin"]
    
    for src_dir in bin_source_dirs:
        if src_dir.exists() and src_dir.is_dir():
            for item in src_dir.iterdir():
                dest = BIN_DIR / item.name
                if dest.is_symlink() and str(dest.resolve()) == str(item.resolve()):
                    os.unlink(dest)
                    print(f"Removed link {dest}")

    shutil.rmtree(db[pkg]["path"], ignore_errors=True)
    del db[pkg]
    save_db(db)
    print(f"Removed {pkg}.")

def upgrade_packages():
    db = load_db()
    idx = load_index()
    
    to_upgrade = []
    for pkg, info in db.items():
        local_ver = info.get("version", "0.0.0")
        remote_info = idx.get("packages", {}).get(pkg)
        if remote_info:
            remote_ver = remote_info.get("version", "0.0.0")
            if remote_ver > local_ver:
                to_upgrade.append((pkg, remote_ver))
                
    if not to_upgrade:
        print("Everything is up to date.")
        return
        
    print("The following packages will be upgraded:")
    for pkg, ver in to_upgrade:
        print(f"  {pkg} ({db[pkg].get('version', '0.0.0')} -> {ver})")
        
    for pkg, ver in to_upgrade:
        print(f"Upgrading {pkg}...")
        data = fetch_package(pkg)
        if not data:
            print(f"Failed to fetch {pkg}. Skipping.")
            continue
        _install_single(pkg, db, data, "yapm")
        print(f"Upgraded {pkg}.")

def list_installed():
    db = load_db()
    if not db:
        print("No packages installed.")
        return
    for pkg, info in db.items():
        ver = info.get("version", "0.0.0")
        fmt = info.get("format", "yapm")
        print(f"{pkg} (v{ver}) [{fmt.upper()}]")

def uninstall_yapm():
    if os.getuid() != 0:
        print("Error: uninstalling system YAPM requires sudo.")
        # Try to uninstall user version if running without sudo
        std_bin = Path.home() / ".local" / "bin" / "yapm"
        if std_bin.exists():
            os.unlink(std_bin)
            shutil.rmtree(HOME / ".config" / "yapm", ignore_errors=True)
            shutil.rmtree(HOME / ".local" / "share" / "yapm", ignore_errors=True)
            print("Successfully uninstalled user-level yapm.")
            return
        sys.exit(1)

    print("Uninstalling system-wide yapm...")
    script_path = Path(__file__).resolve()
    if "bin/yapm" in str(script_path):
        os.unlink(script_path)
    else:
        std_bin = Path("/usr/local/bin/yapm")
        if std_bin.exists():
            os.unlink(std_bin)

    shutil.rmtree("/etc/yapm", ignore_errors=True)
    shutil.rmtree("/var/lib/yapm", ignore_errors=True)
    print("Successfully uninstalled yapm.")

def info_package(pkg: str):
    idx = load_index()
    db = load_db()
    
    print(f"Package: {pkg}")
    
    if pkg in db:
        print(f"Status: Installed (v{db[pkg].get('version', '0.0.0')}) [Format: {db[pkg].get('format', 'yapm').upper()}]")
        meta = db[pkg].get("metadata", {})
        if "description" in meta:
            print(f"Description: {meta['description']}")
        if "dependencies" in meta and meta["dependencies"]:
            print(f"Dependencies: {', '.join(meta['dependencies'])}")
    else:
        print("Status: Not installed")
        
    if pkg in idx.get("packages", {}):
        remote = idx["packages"][pkg]
        print(f"Remote Version: {remote.get('version', '0.0.0')}")
        if "dependencies" in remote and remote["dependencies"]:
            print(f"Remote Dependencies: {', '.join(remote['dependencies'])}")
    else:
        print("Not found in remote index.")

def search_package(term: str):
    idx = load_index()
    found = False
    term_lower = term.lower()
    
    for pkg_name, pkg_info in idx.get("packages", {}).items():
        desc = pkg_info.get("description", "").lower()
        if term_lower in pkg_name.lower() or term_lower in desc:
            ver = pkg_info.get("version", "0.0.0")
            print(f"{pkg_name} (v{ver}) - {pkg_info.get('description', 'No description')}")
            found = True
            
    if not found:
        print("No matches found in local index. Try 'yapm update' first.")

def build_package(directory: str):
    source_dir = Path(directory)
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"Error: Directory '{directory}' does not exist.")
        sys.exit(1)
        
    yapm_data_path = source_dir / "yapm.data"
    if not yapm_data_path.exists():
        print(f"Error: No yapm.data found in '{directory}'. Cannot build package.")
        sys.exit(1)
        
    with open(yapm_data_path) as f:
        y_data = parse_yapm_data(f.read())
        
    name = y_data.get("METADATA", {}).get("name", source_dir.name)
    version = y_data.get("METADATA", {}).get("version", "0.0.0")
    
    out_file = f"{name}-{version}.yapm"
    print(f"Building {out_file} from {directory}...")
    
    with zipfile.ZipFile(out_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(source_dir)
                zf.write(file_path, arcname)
                
    print(f"Success! Package built: {out_file}")

# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(prog="yapm", description="Yet Another Package Manager (Universal)")
    parser.add_argument("-f", "--format", choices=["yapm", "deb", "arch"], default="yapm", help="Specify the package format")
    parser.add_argument("-S", "--system", action="store_true", help="Perform system-wide operation (requires sudo)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Install a package")
    p_install.add_argument("package")

    p_remove = sub.add_parser("remove", help="Remove a package")
    p_remove.add_argument("package")

    sub.add_parser("list", help="List installed packages")

    p_info = sub.add_parser("info", help="Get info about a package")
    p_info.add_argument("package")

    p_search = sub.add_parser("search", help="Search for packages in the index")
    p_search.add_argument("term")

    sub.add_parser("update", help="Update the local package index from mirrors")
    sub.add_parser("upgrade", help="Upgrade all installed packages to their latest versions")
    sub.add_parser("version", help="Show version info")
    sub.add_parser("uninstall", help="Uninstall YAPM itself")

    p_build = sub.add_parser("build", help="Build a .yapm package from a directory containing yapm.data")
    p_build.add_argument("directory", help="The directory containing the package files and yapm.data")

    p_mirror = sub.add_parser("mirror", help="Manage mirrors")
    mirror_sub = p_mirror.add_subparsers(dest="mirror_cmd", required=True)
    
    m_add = mirror_sub.add_parser("add", help="Add a new mirror")
    m_add.add_argument("url", help="URL of the mirror")
    m_add.add_argument("-p", "--priority", type=int, default=10, help="Priority (lower is better, default: 10)")
    
    mirror_sub.add_parser("list", help="List all mirrors")
    
    m_remove = mirror_sub.add_parser("remove", help="Remove a mirror")
    m_remove.add_argument("url", help="URL of the mirror to remove")
    
    mirror_sub.add_parser("sync", help="Check/refresh mirror status")

    args = parser.parse_args()

    # Apply configuration based on system flag
    configure_paths(args.system)
    ensure_dirs()

    if args.command == "install":
        install_package(args.package, args.format)
    elif args.command == "remove":
        remove_package(args.package)
    elif args.command == "list":
        list_installed()
    elif args.command == "info":
        info_package(args.package)
    elif args.command == "search":
        search_package(args.term)
    elif args.command == "update":
        update_index()
    elif args.command == "upgrade":
        upgrade_packages()
    elif args.command == "build":
        build_package(args.directory)
    elif args.command == "version":
        ver = "unknown"
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                ver = json.load(f).get("version", "unknown")
        print(f"yapm version {APP_VERSION}")
        print(f"config version {ver}")
    elif args.command == "uninstall":
        uninstall_yapm()
    elif args.command == "mirror":
        if args.mirror_cmd == "add":
            mirror_add(args.url, args.priority)
        elif args.mirror_cmd == "remove":
            mirror_remove(args.url)
        elif args.mirror_cmd == "sync":
            mirror_refresh()
        elif args.mirror_cmd == "list":
            mirror_list()

if __name__ == "__main__":
    main()
