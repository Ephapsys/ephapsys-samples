#!/usr/bin/env bash
# Offline flow test for run_gcp.sh with PERSONALIZE_ANCHOR=hsm (PKCS#11). No VM is created:
# gcloud is stubbed and logs every call; enroll_device.py is stubbed. Asserts, on a REUSED VM, that the
# idempotent PKCS#11 bootstrap and enrollment run before the agent starts (background and interactive),
# that a failed enrollment never starts the agent, and that non-hsm reuse keeps the fast path.
set -u
SAMPLE=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/pkg/ephapsys"
printf 'from setuptools import setup\nsetup(name="ephapsys", version="0.3.0", packages=["ephapsys"])\n' > "$T/pkg/setup.py"; : > "$T/pkg/ephapsys/__init__.py"
python3 -m venv "$T/venv" >/dev/null && "$T/venv/bin/pip" install -q "$T/pkg" >/dev/null 2>&1
cat > "$T/bin/gcloud" <<'G'
#!/usr/bin/env bash
echo "gcloud $*" >> "$STUB_LOG"
case "$*" in
  *"config get-value account"*) echo "tester@example.com" ;;
  *"config get-value project"*) echo "proj" ;;
  *"projects describe"*"projectId"*) echo "proj" ;;
  *"projects describe"*) echo "12345" ;;
  *"compute scp"*"pkcs11_enrollment.json"*) for a in "$@"; do case "$a" in /*) dst="$a";; esac; done
      echo '{"device_id":"vm","pubkey_pem":"x","sign_spki_sha256":"y"}' > "$dst" ;;
  *"tar -xf"*) cat >/dev/null ;;
  *"compute scp"*"/.env "*|*"compute scp"*":~/helloworld/.env"*) cp "$3" "$STUB_LOG.env" 2>/dev/null || true ;;
esac
exit 0
G
chmod +x "$T/bin/gcloud"
fail=0
run() {  # anchor interactive enroll_rc
  local W="$T/w-$1-$2-$3"; cp -R "$SAMPLE" "$W"; rm -rf "$W/.venv" "$W/.env" "$W/.env.gcp" "$W/.last_gcp_instance"
  printf 'import sys\nopen("%s/log","a").write("ENROLL\\n")\nsys.exit(%s)\n' "$W" "$3" > "$W/pkcs11/enroll_device.py"
  printf 'PROJECT_ID=proj\nZONE=us-central1-a\nMACHINE_TYPE=e2-standard-4\nDISK_SIZE=50\nIMAGE_FAMILY=ubuntu-2204-lts\nIMAGE_PROJECT=ubuntu-os-cloud\nINSTANCE_PREFIX=hello\n' > "$W/.env.gcp"
  printf 'AOC_API_URL=https://aoc.example.test\nAOC_BASE_URL=https://aoc.example.test\nAOC_ORG_ID=org\nAOC_PROVISIONING_TOKEN=boot_x\nAGENT_TEMPLATE_ID=tpl-1\nPERSONALIZE_ANCHOR=%s\nPKCS11_TOKEN_LABEL="Hello World"\n' "$1" > "$W/.env"
  printf 'INSTANCE_NAME=hello-existing\nPROJECT_ID=proj\nZONE=us-central1-a\nANCHOR=%s\n' "$1" > "$W/.last_gcp_instance"
  local flag=--no-interactive; [ "$2" = true ] && flag=--interactive
  STUB_LOG="$W/log" PATH="$T/bin:$PATH" HELLOWORLD_VENV="$T/venv" bash "$W/run_gcp.sh" $flag >"$W/out" 2>&1; echo $? > "$W/rc"
  echo "$W"
}
order() {  # file pattern... : patterns must appear in this order
  local f=$1; shift; local last=0 n
  for p in "$@"; do n=$(grep -n -m1 -E -- "$p" "$f" | cut -d: -f1); if [ -z "$n" ] || [ "$n" -le "$last" ]; then echo "  order violated at: $p"; return 1; fi; last=$n; done
}
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }

W=$(run hsm false 0)
check "hsm reuse/background: bootstrap -> record -> enroll -> start" \
  "order $W/log '\.bootstrap\.sh' 'pkcs11_enrollment.json' '^ENROLL' 'nohup python helloworld_agent' && [ \$(cat $W/rc) = 0 ]"
check "hsm reuse: bootstrap defers the bot start" "grep -q 'bot start deferred' $W/log"
check "hsm reuse: uploaded VM env is shell-quoted and round-trips" \
  "[ \"\$(env -i HOME=/home/vm bash -c 'set -a; source $W/log.env; echo \"\$PKCS11_TOKEN_LABEL|\$SOFTHSM2_CONF\"')\" = 'Hello World|/home/vm/helloworld/.softhsm/softhsm2.conf' ]"
W=$(run hsm true 0)
check "hsm reuse/interactive: enroll before interactive session" "order $W/log '\.bootstrap\.sh' '^ENROLL' '-- -t cd ~/helloworld' && [ \$(cat $W/rc) = 0 ]"
W=$(run hsm false 1)
check "hsm enroll failure: agent never started, exit 1" "! grep -q 'nohup python helloworld_agent' $W/log && [ \$(cat $W/rc) = 1 ]"
W=$(run none false 0)
check "non-hsm reuse keeps the fast path" "grep -q 'test -x ~/helloworld/.venv/bin/python' $W/log && ! grep -q '\.bootstrap\.sh' $W/log"
exit $fail
