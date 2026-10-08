"""Build a control-only Lambda zip. Does not upload it or touch the live book.

The zip keeps the seven-feature model contract. It does not contain sklearn or xgboost.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FILES = [ROOT / "lambda_function.py", *sorted((ROOT / "swing_core").glob("*.py"))]


def main() -> None:
    DIST.mkdir(exist_ok=True)
    bundle = DIST / "control_candidate.zip"
    manifest = {"deployed": False, "contains_sklearn": False, "contains_xgboost": False, "files": []}
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in FILES:
            if path.name == "__init__.py" or path.suffix == ".py":
                rel = path.relative_to(ROOT).as_posix()
                archive.write(path, rel)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest["files"].append({"path": rel, "sha256": digest})
    manifest["zip_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    manifest["rollback_code_sha256"] = "TOeunud6CMQm+iDnmWNa0FH30+gp5RedddyS8JPxgJg="
    manifest["rollback_model_sha256"] = "fac68a53e0776ac1be739d2c08d5c15981a04150d73c0bfa5fdcbfa09dab1cb9"
    manifest["notes"] = "Local candidate only. Production Lambda and S3 portfolio_state were not changed."
    (DIST / "control_candidate_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"zip": str(bundle), "files": len(manifest["files"]), "deployed": False}, indent=2))


if __name__ == "__main__":
    main()
