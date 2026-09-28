"""pkcs11/provision_token.py against SoftHSM (software token; skipped if SoftHSM/python-pkcs11 are absent).

Run: pytest agents/helloworld/tests/test_pkcs11_provision.py   (needs ephapsys[pkcs11] >= 0.3.0)
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pkcs11 = pytest.importorskip("pkcs11")
pytest.importorskip("ephapsys.crypto.pkcs11")
LIB = next((p for p in (os.getenv("SOFTHSM2_LIB", ""), "/opt/homebrew/lib/softhsm/libsofthsm2.so",
                        "/usr/lib/softhsm/libsofthsm2.so", "/usr/local/lib/softhsm/libsofthsm2.so") if p and os.path.exists(p)), None)
if not LIB or not shutil.which("softhsm2-util"):
    pytest.skip("SoftHSM not installed", allow_module_level=True)

SCRIPT = Path(__file__).resolve().parents[1] / "pkcs11" / "provision_token.py"


@pytest.fixture
def env(tmp_path):
    conf = tmp_path / "softhsm2.conf"; (tmp_path / "tokens").mkdir()
    conf.write_text(f"directories.tokendir = {tmp_path / 'tokens'}\nobjectstore.backend = file\nlog.level = ERROR\n")
    e = {**os.environ, "SOFTHSM2_CONF": str(conf), "PKCS11_MODULE": LIB, "PKCS11_TOKEN_LABEL": "hw",
         "PKCS11_PIN_FILE": str(tmp_path / "secrets" / "pin"), "PKCS11_SIGN_KEY_LABEL": "hw-sign",
         "PKCS11_KEM_KEY_LABEL": "hw-kem", "EPHAPSYS_DEVICE_ID": "dev-1"}
    for k in ("PKCS11_PIN", "PKCS11_TOKEN_SERIAL", "PKCS11_SIGN_KEY_ID", "PKCS11_KEM_KEY_ID"):
        e.pop(k, None)
    return e


def run(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)


def objects(env, label="hw"):
    """Count key objects on the token (fresh process so SoftHSM re-reads its token directory)."""
    code = ("import os,pkcs11,sys;from pkcs11 import ObjectClass,Attribute;"
            "t=pkcs11.lib(os.environ['PKCS11_MODULE']).get_token(token_label=sys.argv[1]);"
            "s=t.open(user_pin=open(os.environ['PKCS11_PIN_FILE']).read().strip());"
            "print(len(list(s.get_objects({Attribute.CLASS:ObjectClass.PRIVATE_KEY})))+len(list(s.get_objects({Attribute.CLASS:ObjectClass.PUBLIC_KEY}))))")
    return int(subprocess.run([sys.executable, "-c", code, label], env=env, capture_output=True, text=True, check=True).stdout)


def init_token(env, label="hw"):
    Path(env["PKCS11_PIN_FILE"]).parent.mkdir(parents=True, exist_ok=True)
    if not Path(env["PKCS11_PIN_FILE"]).exists():
        fd = os.open(env["PKCS11_PIN_FILE"], os.O_WRONLY | os.O_CREAT, 0o600); os.write(fd, b"1234\n"); os.close(fd)
    pin = Path(env["PKCS11_PIN_FILE"]).read_text().strip()
    subprocess.run(["softhsm2-util", "--init-token", "--free", "--label", label, "--pin", pin, "--so-pin", "5678"],
                   env=env, check=True, capture_output=True)


def test_softhsm_init_creates_pin_0600_and_role_separated_keys(env):
    r = run(env, "--init-softhsm")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["device_id"] == "dev-1" and rec["softhsm_dev_token"] is True and rec["sign_spki_sha256"] != rec["kem_spki_sha256"]
    assert stat.S_IMODE(os.stat(env["PKCS11_PIN_FILE"]).st_mode) == 0o600
    assert objects(env) == 4
    r2 = run(env)                                           # idempotent
    assert r2.returncode == 0 and json.loads(r2.stdout)["sign_spki_sha256"] == rec["sign_spki_sha256"] and objects(env) == 4


def test_serial_mismatch_writes_nothing(env):
    init_token(env)
    r = run({**env, "PKCS11_TOKEN_SERIAL": "0000000000000000"})
    assert r.returncode != 0 and "exactly one token" in (r.stderr + r.stdout)
    assert objects(env) == 0


def test_ambiguous_token_label_writes_nothing(env):
    init_token(env); init_token(env)                        # two tokens with the same label
    r = run(env)
    assert r.returncode != 0 and "exactly one token" in (r.stderr + r.stdout)


def test_configured_ids_round_trip(env):
    init_token(env)
    e = {**env, "PKCS11_SIGN_KEY_ID": "0a", "PKCS11_KEM_KEY_ID": "0b"}
    r = run(e)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    ids_only = {k: v for k, v in e.items() if k not in ("PKCS11_SIGN_KEY_LABEL", "PKCS11_KEM_KEY_LABEL")}
    r2 = run(ids_only)                                      # resolves the same keys by CKA_ID alone, creates nothing
    assert r2.returncode == 0 and json.loads(r2.stdout)["kem_spki_sha256"] == rec["kem_spki_sha256"] and objects(env) == 4


def test_ambiguous_key_selector_refused_without_writing(env):
    init_token(env)
    code = ("import os,pkcs11;from pkcs11 import KeyType,Attribute;from pkcs11.util.ec import encode_named_curve_parameters as p;"
            "t=pkcs11.lib(os.environ['PKCS11_MODULE']).get_token(token_label='hw');"
            "s=t.open(user_pin=open(os.environ['PKCS11_PIN_FILE']).read().strip(),rw=True);"
            "d=s.create_domain_parameters(KeyType.EC,{Attribute.EC_PARAMS:p('secp256r1')},local=True);"
            "[d.generate_keypair(store=True,label='hw-sign') for _ in range(2)]")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)
    before = objects(env)
    r = run(env)
    assert r.returncode != 0 and "more than one key" in r.stderr
    assert objects(env) == before                           # KEM key not created either


def test_world_readable_pin_file_rejected(env):
    init_token(env)
    os.chmod(env["PKCS11_PIN_FILE"], 0o644)
    r = run(env)
    assert r.returncode != 0 and "group/others" in r.stderr
    assert objects(env) == 0
