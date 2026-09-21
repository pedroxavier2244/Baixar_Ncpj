#!/bin/bash
# vigia-cnpj.sh — avisa quando o coletor do Mac mini EMUDECE. Roda de hora em hora.
#
# POR QUE EXISTE: desde 21/09/2026 quem baixa os dados da Receita e um Mac mini no
# escritorio, porque a VPS teve o IP bloqueado pela RF. O coletor loga tudo — mas o
# log fica LA. Se o Mac mini estiver desligado, sem rede, ou com o LaunchDaemon
# parado, nao existe erro em lugar nenhum: o mes simplesmente nao entra, e a API
# segue servindo o mes anterior sem reclamar. Silencio se parece com "a Receita
# ainda nao publicou".
#
# Mesmo raciocinio do vigia.sh do mb-resultado, e usa a mesma bridge.
#
# DOIS GATILHOS, com velocidades diferentes:
#   1. batimento velho (> MAX_HORAS)  -> Mac mini mudo. Detecta em ~2 dias.
#   2. ultimo batimento diz "falha"   -> o coletor rodou e quebrou. Detecta em ~1h.
# O segundo e o que mais vale: se a RF passar a bloquear IP residencial tambem, da
# para saber no mesmo dia em vez de dois dias depois.
#
#   vigia-cnpj.sh --producao   |   vigia-cnpj.sh --dry-run
set -u

# Tudo aceita override por variavel de ambiente, para dar para exercitar os
# caminhos de alarme sem mexer no batimento de verdade nem mandar mensagem.
BATIMENTO=${CNPJ_VIGIA_BATIMENTO:-/var/lib/docker/volumes/cnpj_etl_checkpoints/_data/coletor_batimento.json}
ESTADO=${CNPJ_VIGIA_ESTADO:-/var/lib/cnpj-vigia/ultimo-alerta}
LOG=${CNPJ_VIGIA_LOG:-/var/log/cnpj-vigia.log}
BRIDGE=${CNPJ_VIGIA_BRIDGE:-http://127.0.0.1:3131}
TZ_BR=America/Sao_Paulo

# ALERTA TECNICO NAO VAI PRO GRUPO — mesma regra do vigia.sh do mb-resultado.
# Este numero e so do CNPJ, diferente do destino dos alertas da esteira.
DEST=${CNPJ_VIGIA_DEST:-5521971506193@s.whatsapp.net}

MAX_HORAS=${CNPJ_VIGIA_MAX_HORAS:-48}   # o coletor roda 1x/dia; 48h tolera um tropeco sem alarme falso

MODO=""
for a in "$@"; do
  case "$a" in
    --producao) MODO="producao" ;;
    --dry-run)  MODO="dry" ;;
    *) echo "argumento desconhecido: $a" >&2; exit 2 ;;
  esac
done
[ -z "$MODO" ] && { echo "use --producao ou --dry-run; rodar sem flag e proibido" >&2; exit 2; }

mkdir -p "$(dirname "$ESTADO")"
log(){ echo "$(TZ=$TZ_BR date '+%F %T') $*" >> "$LOG"; }

# Envia e confere de verdade. `curl -s` sozinho sai com 0 ate em HTTP 500 — o
# vigia registraria "alerta enviado" para uma mensagem que a bridge recusou, que
# e a pior falha possivel aqui: silencio com aparencia de funcionamento.
enviar(){
  local texto="$1" corpo resposta codigo
  corpo=$(python3 -c "import json,sys;print(json.dumps({'to':'$DEST','text':sys.stdin.read()}))" <<<"$texto")
  resposta=$(curl -s -m 60 -w '\n%{http_code}' -X POST "$BRIDGE/send" \
             -H 'Content-Type: application/json' --data "$corpo" 2>&1)
  codigo=$(tail -n1 <<<"$resposta")
  if [ "$codigo" = "200" ]; then
    log "VIGIA-CNPJ | enviado (HTTP $codigo)"
    return 0
  fi
  log "VIGIA-CNPJ | FALHOU ao enviar (HTTP ${codigo:-sem-resposta}): $(sed '$d' <<<"$resposta" | tr -d '\n' | cut -c1-200)"
  return 1
}

problemas=()

# 1) existe batimento?
if [ ! -f "$BATIMENTO" ]; then
  problemas+=("nunca recebi batimento do Mac mini (arquivo nao existe)")
else
  # 2) o batimento e recente? E o que ele diz?
  leitura=$(python3 - "$BATIMENTO" "$MAX_HORAS" <<'PY'
import json, sys
from datetime import datetime, timezone

caminho, max_horas = sys.argv[1], float(sys.argv[2])
try:
    d = json.load(open(caminho, encoding="utf-8"))
except Exception as exc:
    print(f"ERRO|batimento ilegivel ({type(exc).__name__})")
    raise SystemExit(0)

try:
    ts = datetime.fromisoformat(d["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    horas = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
except Exception:
    print("ERRO|batimento sem data utilizavel")
    raise SystemExit(0)

if horas > max_horas:
    print(f"ERRO|sem batimento ha {horas:.0f}h (limite {max_horas:.0f}h) — "
          f"ultimo em {d.get('ts')}, Mac mini desligado, sem rede ou daemon parado")
elif d.get("resultado") != "ok":
    print(f"ERRO|o coletor rodou e FALHOU: {d.get('detalhe')} "
          f"(acao={d.get('acao')}, mes={d.get('run_key')}, ha {horas:.0f}h)")
else:
    print(f"OK|batimento de {horas:.0f}h atras: {d.get('acao')} "
          f"mes={d.get('run_key')} em {d.get('duracao_min')}min")
PY
)
  estado="${leitura%%|*}"
  texto="${leitura#*|}"
  [ "$estado" = "ERRO" ] && problemas+=("$texto")
fi

# 3) a bridge esta conectada? Sem ela, nada disso chega a ninguem.
curl -s -m 10 "$BRIDGE/health" 2>/dev/null | grep -q conectado \
  || problemas+=("a bridge do WhatsApp NAO esta conectada — alertas nao saem")

# ── Deduplicacao ────────────────────────────────────────────────────────────
# Roda de hora em hora; sem isto, um Mac mini desligado no fim de semana renderia
# 48 mensagens iguais. Guarda a assinatura do problema e so fala quando ela muda.
assinatura=$(printf '%s\n' "${problemas[@]:-}" | md5sum | cut -d' ' -f1)
anterior=$(cat "$ESTADO" 2>/dev/null || echo "")

if [ ${#problemas[@]} -eq 0 ]; then
  if [ -n "$anterior" ] && [ "$anterior" != "vazio" ]; then
    MSG="✅ *Coletor CNPJ voltou ao normal* ($(TZ=$TZ_BR date '+%d/%m %H:%M'))

$texto"
    log "VIGIA-CNPJ | recuperado"
    [ "$MODO" = "dry" ] && { echo "--- DRY-RUN ---"; echo "$MSG"; exit 0; }
    # O estado so e limpo se a mensagem saiu; senao, tenta de novo na proxima hora.
    if enviar "$MSG"; then
      echo "vazio" > "$ESTADO"
      echo "recuperado — aviso enviado"
    else
      echo "recuperado — mas o aviso NAO saiu; tenta de novo na proxima hora"
    fi
    exit 0
  fi
  log "VIGIA-CNPJ | tudo certo: $texto"
  echo "vazio" > "$ESTADO"
  echo "ok: $texto"
  exit 0
fi

log "VIGIA-CNPJ | ${#problemas[@]} problema(s): ${problemas[*]}"

if [ "$assinatura" = "$anterior" ]; then
  echo "${#problemas[@]} problema(s) — ja avisado, silencio ate mudar"
  exit 0
fi

MSG="🔴 *Coletor CNPJ com problema* ($(TZ=$TZ_BR date '+%d/%m %H:%M'))"
for p in "${problemas[@]}"; do MSG="$MSG
• $p"; done
MSG="$MSG

_(aviso automatico do vigia-cnpj — ver /var/log/cnpj-vigia.log na VPS)_"

if [ "$MODO" = "dry" ]; then echo "--- DRY-RUN ---"; echo "$MSG"; exit 0; fi

# A assinatura so e gravada se a mensagem saiu. Se a bridge estiver fora, o
# alerta nao vira "ja avisado" e sai de verdade na proxima hora.
if enviar "$MSG"; then
  echo "$assinatura" > "$ESTADO"
  echo "${#problemas[@]} problema(s) — alerta enviado"
else
  echo "${#problemas[@]} problema(s) — alerta NAO saiu; tenta de novo na proxima hora"
fi
