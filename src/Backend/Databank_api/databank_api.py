from flask import Flask, request, abort
import os, json, subprocess
from pathlib import Path
from fairmd.lipids.schema_validation.validate_info_dict import parse_valid_config_settings
from fairmd.lipids import FMDL_MOL_PATH
import fairmd.lipids.molecules as molecules
import importlib
import logging
import requests
from api_return_standard import api_return
from fairmd.lipids.schema_validation.validate_yaml import validate_info_dict

app = Flask(__name__)
# Paths and filenames
BILAYERDATA_PATH = os.getenv("BILAYERDATA_PATH", "/app/BilayerData")
DATABANK_PATH = os.getenv("DATABANK_PATH", "/app/Databank")
LOCAL_STATIC = os.getenv("LOCAL_STATIC", "/app/static")
MOLECULE_FILE = os.path.join(LOCAL_STATIC, "molecules.json")
MAPPING_FILE = os.path.join(LOCAL_STATIC,"mapping-files.json")
GITHUB_GATEWAY_URL = os.getenv("GITHUB_GATEWAY_URL", "http://github_gateway:5001")

# Ensure local static folder exists
os.makedirs(LOCAL_STATIC, exist_ok=True)

lipids_set    = molecules.lipids_set
molecules_set = molecules.solubles_set

logger = logging.getLogger('gunicorn.error')
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def pull_latest() -> None:
    """
    Pull latest changes for BilayerData and Databank repositories.
    """
    repos = [
        (BILAYERDATA_PATH, "BilayerData"),
        (DATABANK_PATH, "Databank"),
    ]

    for path, name in repos:
        logger.info("Pulling latest %s repo at %s", name, path)
        try:
            subprocess.run(["git", "pull"], cwd=path, check=True)
        except subprocess.CalledProcessError as e:
            logger.error("Failed to update %s repository: %s", name, e)
            raise
            
def ensure_fairmd_schema_symlink_compat():
    """
    Compatibility shim for FAIRMD_lipids layout changes.
    Some installed fairmd validation code may still reference:
      /app/Databank/src/fairmd/lipids/schema_validation/schema/info_yml_schema.json
    We create that path as a symlink to the actual installed package location
    when possible.
    """
    legacy_schema = Path("/app/Databank/src/fairmd/lipids/schema_validation/schema/info_yml_schema.json")
    if legacy_schema.exists():
        return

    try:
        import fairmd.lipids as fairmd_lipids_pkg
        pkg_root = Path(fairmd_lipids_pkg.__file__).resolve().parent
        current_schema = pkg_root / "schema_validation" / "schema" / "info_yml_schema.json"
        if not current_schema.exists():
            logger.warning("FAIRMD schema not found at expected package path: %s", current_schema)
            return

        legacy_schema.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy_schema.symlink_to(current_schema)
            logger.info("Created FAIRMD schema compatibility symlink: %s -> %s", legacy_schema, current_schema)
        except FileExistsError:
            pass
    except Exception as e:
        logger.warning("Could not prepare FAIRMD schema path compatibility shim: %s", e)

def refresh_molecule_file():
    "Refreshes the local molecules.json file with updated molecule names."
    logger.info("Refreshing molecule file")
    importlib.reload(molecules)

    global lipids_set, solubles_set
    lipids = sorted(molecules.lipids_set.names)
    solution = sorted(molecules.solubles_set.names)

    all_ids = {"lipids": lipids, "solution": solution}

    out_path = MOLECULE_FILE
    tmp_path = out_path + ".tmp"

    with open(tmp_path, 'w') as f:
        json.dump(all_ids, f, indent=2)
    os.replace(tmp_path, out_path)

    logger.info("Wrote %d molecules to %s", len(all_ids), MOLECULE_FILE)
    return len(all_ids)


def build_mapping_dict(base_path=FMDL_MOL_PATH):
    logger.info("Building mapping dict from %s", base_path)
    mapping_dict = {}
    lipid_path = os.path.join(base_path, "membrane")
    non_lipid_path = os.path.join(base_path, "solution")
    for base_dir in (lipid_path, non_lipid_path):
        for molecule in os.listdir(base_dir):
            mol_dir = os.path.join(base_dir, molecule)
            for fname in os.listdir(mol_dir):
                if "mapping" in fname:
                    mapping_dict.setdefault(molecule, []).append(fname)
    for mapping_list in mapping_dict.values():
        mapping_list.sort()

    tmp = MAPPING_FILE + ".tmp"
    with open(tmp, "w") as fp:
        json.dump(mapping_dict, fp, indent=2)
    os.replace(tmp, MAPPING_FILE)

    logger.info("Wrote %d mappings to %s", len(mapping_dict), MAPPING_FILE)
    return len(mapping_dict)

@app.route("/mapping-files", methods=["GET"])
def list_mappings():
    """Return cached mapping files from mapping-files.json."""
    try:
        with open(MAPPING_FILE, "r") as fp:
            mapping_dict = json.load(fp)
    except FileNotFoundError as e:
        logger.error("Mapping file not found: %s", MAPPING_FILE)
        return api_return(error="Mapping file not available", status=404)
    return api_return(payload=mapping_dict)


@app.route('/molecules', methods=['GET'])
def list_molecules():
    """
    GET /molecules - returns local list of molecules.
    """
    with open(MOLECULE_FILE, 'r') as f:
        names = json.load(f)
    return api_return(payload=names)


@app.route("/refresh-databank-files", methods=["POST"])
def refresh_databank_files():
    """
    Pulls latest Databank repo, updates submodules,
    rebuilds molecules.json and mapping-files.json.
    """
    #Permission check
    err = admin_check()
    if err:
        logger.error(f"Admin check failed with error: {err}")
        return err

    #Pull & update submodules
    try:
        pull_latest()
    except subprocess.CalledProcessError as e:
        logger.exception("Git pull/submodule update failed")
        return api_return(error="Failed to pull Databank repository", status=500)
    except Exception as e:
        logger.exception("Unexpected error during repo pull")
        return api_return(error="Error updating Databank repository", status=500)

    #Refresh molecules.json
    try:
        refresh_molecule_file()
    except FileNotFoundError as e:
        logger.exception("Molecules file path error")
        return api_return(error="Molecule file path invalid", status=500)
    except Exception as e:
        logger.exception("Refreshing molecules failed")
        return api_return(error="Failed to refresh molecules list", status=500)

    #Rebuild mapping-files.json
    try:
        build_mapping_dict(FMDL_MOL_PATH)
    except FileNotFoundError as e:
        logger.exception("Mapping directory missing")
        return api_return(error="Mapping source directory not found", status=500)
    except json.JSONDecodeError as e:
        logger.exception("Error writing mapping JSON")
        return api_return(error="Failed to write mapping-files.json", status=500)
    except Exception as e:
        logger.exception("Building mapping dict failed")
        return api_return(error="Failed to rebuild mapping dictionary", status=500)

    return api_return(payload={"status":"success"})


def admin_check():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return api_return(error="Missing token", status=401)

    headers = {'Authorization': auth}
    logger.info("Checking user admin status")
    try:
        resp = requests.post(f"{GITHUB_GATEWAY_URL}/user-admin-check", headers=headers,timeout=10)
        resp.raise_for_status()
    except requests.HTTPError as e:
        logger.error(f"User admin check failed, error:{e}")
        return api_return(error=resp.json().get("error", str(e)), status=resp.status_code)
    except requests.RequestException as e:
        logger.error("HTTP error during admin check: %s", e)
        return api_return(error="Could not reach admin‑check service", status=502)

    data = resp.json()  
    if not data.get("authorized", False):
        return api_return(error="Insufficient privileges", status=403)
    return None

@app.route('/info-valid-check', methods=['POST'])
def info_valid_check():
    """
    POST /info-valid-check - validates provided YAML dict.
    """
    data = request.get_json()
    if not data:
        abort(400, description="Invalid or missing JSON payload")
    try:
        parse_valid_config_settings(data,logger)
    except Exception as e:
        logger.error("Validation failed: %s", e)
        return api_return(error=str(e), status=400)
    try:
        logger.info("Validating info file by schema")
        errors = validate_info_dict(data) or []
        if errors:
            msg = format_schema_errors(errors)
            logger.error("Schema validation failed:\n%s", msg)
            return api_return(error=msg, status=400)

        logger.info("No errors found in info file by schema")

    except Exception as e:
        logger.error("Schema validation crashed: %s", e)
        return api_return(error=str(e), status=400)

    return api_return(payload={"valid": True}, status=200)
        
def format_schema_errors(errors) -> str:
    lines = []
    for e in errors:
        path = ".".join(str(p) for p in e.path) if e.path else "<root>"
        lines.append(f"{path}: {e.message}")
    return "\n".join(lines)


@app.route('/health', methods=['GET'])
def health_check():
    return api_return(payload={"status":"ok"})

# Initial setup, runs when gunicorn imports this app:
ensure_fairmd_schema_symlink_compat()
pull_latest()
refresh_molecule_file()
build_mapping_dict(FMDL_MOL_PATH)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
