#!/usr/bin/env bash
# ACN_ON_QUOTE_MODEL_PRICES hook for Comiclaw / TokenPlan.
# stdin: {"model_ids":[...],"retry":bool}
# stdout: {"model_prices":[{model_id,input_price_per_million,output_price_per_million}]}
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${ACN_PROVIDER_ENV_FILE:-}" ]]; then
  IFS=',' read -r -a _env_files <<< "$ACN_PROVIDER_ENV_FILE"
  for f in "${_env_files[@]}"; do
    f="$(echo "$f" | xargs)"
    [[ -n "$f" && -f "$f" ]] || continue
    set -a; # shellcheck disable=SC1090
    source "$f"; set +a
  done
elif [[ -f "$HOME/.config/comiclaw/provider.env" ]]; then
  set -a; source "$HOME/.config/comiclaw/provider.env"; set +a
fi
exec python3 "$DIR/quote_model_prices.py"
