#!/usr/bin/env bash
# Yayın öncesi güvenlik kapısı (GitHub Actions'tan bağımsız).
#
# main'e her gönderimden ÖNCE çalıştırılır; herhangi bir adım kırmızıysa gönderim YAPILMAZ.
#   bash scripts/predeploy.sh            → yalnız kontrol
#   bash scripts/predeploy.sh --deploy   → kontrol + geçerse main ve çalışma dalına gönderim
#
# Adımlar:
#   1) Değişen Python dosyaları derlenir (py_compile).
#   2) Değişen frontend dosyalarının sözdizimi denetlenir (esbuild, node_modules gerektirmez).
#   3) Statik rota yetki kapısı + güvenlik birim testleri.
#   4) Güvenlik uçtan uca test paketinin TAMAMI (backend/tests/security_e2e).
#   5) Sır taraması: origin/main..HEAD arasındaki commit'ler gitleaks ile (varsa); yoksa basit desen taraması.
#   6) CLAUDE.md ödeme değişmezleri için kritik dosyalar değiştiyse uyarı (canlı test hatırlatması).
#
# Ortam: PYTHON değişkeni test bağımlılıkları kurulu bir yorumlayıcıyı gösterebilir
# (varsayılan: python3). Bağımlılıklar: backend/requirements.txt + backend/requirements-test.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
BRANCH_DEPLOY="${DEPLOY_BRANCH:-claude/atilla-repo-connect-s07sw3}"
DO_DEPLOY=0
[[ "${1:-}" == "--deploy" ]] && DO_DEPLOY=1

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n== %s ==\n' "$*"; }
fail()  { red "KAPI KAPALI: $*"; exit 1; }

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  fail "commit edilmemiş değişiklik var — önce commit edin (test edilen = gönderilen olmalı)"
fi

git fetch -q origin main || fail "origin/main alınamadı"
BASE="$(git merge-base HEAD origin/main)"
CHANGED="$(git diff --name-only "$BASE" HEAD)"

step "1) Python derleme"
PYFILES="$(echo "$CHANGED" | grep -E '\.py$' || true)"
for f in $PYFILES; do
  [[ -f "$f" ]] && { "$PY" -m py_compile "$f" || fail "derlenemedi: $f"; }
done
green "ok ($(echo "$PYFILES" | grep -c . || true) dosya)"

step "2) Frontend sözdizimi"
JSFILES="$(echo "$CHANGED" | grep -E '^frontend/.*\.(js|jsx)$' || true)"
for f in $JSFILES; do
  [[ -f "$f" ]] || continue
  npx -y esbuild@0.24.0 "$f" --loader:.js=jsx --loader:.jsx=jsx >/dev/null 2>&1 || fail "sözdizimi hatası: $f"
done
green "ok ($(echo "$JSFILES" | grep -c . || true) dosya)"

step "3) Rota yetki kapısı + güvenlik birim testleri"
( cd backend && "$PY" -m pytest -q -p no:cacheprovider tests/test_route_guards.py \
    tests/test_security_hardening.py tests/test_security_fetch.py ) || fail "rota/güvenlik birim testleri"

step "4) Güvenlik uçtan uca test paketi (tamamı)"
( cd backend/tests/security_e2e && "$PY" -m pytest -q -p no:cacheprovider -x . ) || fail "security_e2e"

step "5) Sır taraması"
GL="$(command -v gitleaks || true)"
[[ -z "$GL" && -x "${GITLEAKS_BIN:-}" ]] && GL="$GITLEAKS_BIN"
if [[ -n "$GL" ]]; then
  "$GL" git --no-banner --redact --log-opts="$BASE..HEAD" . || fail "gitleaks yeni sır buldu"
else
  # Yedek: eklenen satırlarda yaygın anahtar biçimleri (testler ve örnek değerler hariç).
  HITS="$(git diff "$BASE" HEAD -U0 -- . ':!*tests/*' ':!*.md' ':!*.lock' \
          | grep -E '^\+' \
          | grep -E '(sk_live_[0-9A-Za-z]{10,}|re_[0-9A-Za-z]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abp]-[0-9A-Za-z-]{10,}|ghp_[0-9A-Za-z]{30,}|AIza[0-9A-Za-z_-]{30,})' || true)"
  [[ -n "$HITS" ]] && fail "eklenen satırlarda sır biçimi bulundu (değerler gösterilmiyor)"
  echo "gitleaks yok — desen taraması temiz"
fi

step "6) Ödeme/sipariş kritik dosyaları"
CRIT="$(echo "$CHANGED" | grep -E '^backend/(routes/orders\.py|routes/payment\.py|scheduler\.py)$' || true)"
if [[ -n "$CRIT" ]]; then
  red "UYARI: CLAUDE.md kritik alanı değişti: $(echo $CRIT)"
  red "Deploy sonrası ZORUNLU: canlı kart siparişi testi (awaiting_payment, payment_status!=paid) + smoke."
fi

green "KAPI AÇIK — tüm kontroller geçti ($(git rev-parse --short HEAD))"

if [[ $DO_DEPLOY -eq 1 ]]; then
  for i in 1 2 3 4; do
    git push -q origin "HEAD:main" "HEAD:$BRANCH_DEPLOY" && { green "gönderildi"; exit 0; }
    sleep $((2**i))
  done
  fail "gönderim başarısız (ağ)"
fi
