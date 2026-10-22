"""Exercise deployment decisions with fake commands; never touch Docker or a cluster."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "deploy.sh").read_text()


def _functions(*names):
    return "\n".join(re.search(rf"^{name}\(\) \{{.*?^\}}", SOURCE, re.M | re.S)[0] for name in names)


def test_image_import_compares_configuration_ids_not_manifest_digests(tmp_path):
    sandbox = tmp_path / "images" / "sbx-office"
    sandbox.mkdir(parents=True)
    (sandbox / "Dockerfile").write_text("FROM test")
    script = (
        _functions("build_images")
        + r"""
step() { :; }
ok() { :; }
warn() { :; }
die() { echo "$*" >&2; exit 1; }
run() { "$@"; }
nice() { shift 2; "$@"; }
docker() {
  case "$*" in
    *ailab.content-hash*) echo "$want" ;;
    *Id*) echo sha256:configuration ;;
    save*) echo image-archive ;;
  esac
}
k3s() {
  case "$*" in
    *'images import'*) cat >/dev/null ;;
    *'images ls'*) echo 'ailab-platform:lite application/vnd.oci.image.manifest.v1+json sha256:manifest' ;;
    'crictl inspecti'*) echo '{"status":{"id":"sha256:configuration"}}' ;;
  esac
}
ROOT=$1 PLATFORM=$1/platform SANDBOX_IMAGE=office:lite PLATFORM_IMAGE=ailab-platform:lite SKIP_BUILD=0 DRY=0
build_images
"""
    )
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script, "test", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_failed_manifest_application_stops_deployment():
    script = (
        _functions("deploy_platform")
        + r"""
step() { :; }
ok() { :; }
die() { echo "$*" >&2; exit 1; }
curl() { return 0; }
k() { return 1; }
wait_http() { return 0; }
run() { [ "${1:-}" != kubectl ]; }
DRY=1 PLATFORM=/unused NS=platform OSB_URL=http://unused CATALOG=models.toml KEY_ENV='' MODEL_KEY='' INVITE_CODE=''
REGISTRATION='' KUBECTL=kubectl API_URL=http://unused BROKER_URL=http://unused
deploy_platform
"""
    )
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0, "a failed kubectl apply must never be reported as a successful deployment"
