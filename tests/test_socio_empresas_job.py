"""
Testa os caminhos do socio_empresas_job que NUNCA rodaram.

Roda em container descartavel contra o banco real. Nao encosta em cnpj.* —
os cenarios que precisam de tabela falsa usam um schema proprio (socio_teste),
apontando settings.pg_schema para la. Criar cnpj.rf_socios_new de verdade seria
perigoso: a proxima carga faria CREATE TABLE nesse nome e falharia.
"""
import sys, traceback
import psycopg
from config import settings
import socio_empresas_job as J

FALHAS = []

def checa(nome, cond, detalhe=""):
    marca = "OK  " if cond else "FALHOU"
    if not cond:
        FALHAS.append(nome)
    print(f"{marca} {nome}" + (f" — {detalhe}" if detalhe else ""))

conn = psycopg.connect(settings.postgres_url, autocommit=True)
SCHEMA_REAL = settings.pg_schema

# sincronizar_base faz TRUNCATE, e este teste roda com 3 CNPJs — sem o snapshot
# ele deixaria a base_cnpj REAL com 3 linhas, e todo irmas_na_base viraria zero
# ate a proxima execucao noturna.
with conn.cursor() as cur:
    cur.execute("DROP TABLE IF EXISTS socio.base_cnpj_bkp_teste")
    cur.execute("CREATE TABLE socio.base_cnpj_bkp_teste AS SELECT * FROM socio.base_cnpj")
    cur.execute("SELECT count(*) FROM socio.base_cnpj_bkp_teste")
    BASE_ORIGINAL = cur.fetchone()[0]
print(f"base_cnpj salva para restaurar depois: {BASE_ORIGINAL:,} linhas")

print("=" * 66)
print("1. carga_rf_em_andamento — o caminho True (a protecao inteira)")
print("=" * 66)
with conn.cursor() as cur:
    cur.execute("DROP SCHEMA IF EXISTS socio_teste CASCADE")
    cur.execute("CREATE SCHEMA socio_teste")
    cur.execute("CREATE TABLE socio_teste.rf_socios_new (x int)")

settings.pg_schema = "socio_teste"
checa("detecta carga em andamento (rf_socios_new existe)",
      J.carga_rf_em_andamento(conn) is True)

with conn.cursor() as cur:
    cur.execute("DROP TABLE socio_teste.rf_socios_new")
    cur.execute("CREATE TABLE socio_teste.rf_estabelecimentos_new (x int)")
checa("detecta tambem por rf_estabelecimentos_new",
      J.carga_rf_em_andamento(conn) is True)

with conn.cursor() as cur:
    cur.execute("DROP TABLE socio_teste.rf_estabelecimentos_new")
checa("sem _new devolve False", J.carga_rf_em_andamento(conn) is False)

print()
print("=" * 66)
print("2. conferir_indice — precisa EXPLODIR quando o indice sumir")
print("=" * 66)
with conn.cursor() as cur:
    cur.execute("CREATE TABLE socio_teste.rf_socios (x int)")
try:
    J.conferir_indice(conn)
    checa("lanca erro quando indice ausente", False, "nao lancou")
except RuntimeError as e:
    msg = str(e)
    checa("lanca erro quando indice ausente", True)
    checa("mensagem diz o que fazer",
          "006_socios_buscar_index" in msg and "load_step" in msg)

settings.pg_schema = SCHEMA_REAL
try:
    J.conferir_indice(conn)
    checa("passa quando o indice existe (schema real)", True)
except Exception as e:
    checa("passa quando o indice existe (schema real)", False, str(e))

print()
print("=" * 66)
print("3. rodar() ponta a ponta — inclui o caminho de RECARGA")
print("=" * 66)
AMOSTRA = ["49005442000381", "22059709000103", "11222333000181"]
J.buscar_cnpjs_da_base = lambda: list(AMOSTRA)   # dispensa o Supabase
settings.socio_job_lote = 2                      # forca mais de um lote

with conn.cursor() as cur:
    cur.execute("DELETE FROM socio.job_controle WHERE chave = 'ultimo_run_key'")
    cur.execute("SELECT valor FROM socio.job_controle WHERE chave='ultimo_run_key'")
    checa("estado inicial: sem run_key gravado", cur.fetchone() is None)

r1 = J.rodar()
print("   ->", r1)
checa("1a execucao: status ok", r1["status"] == "ok")
checa("1a execucao: entrou como RECARGA (run_key era nulo)", r1["recarga"] is True)
checa("1a execucao: devolveu as 3 linhas", r1["linhas"] == len(AMOSTRA))

rk = J.ler_controle(conn, "ultimo_run_key")
checa("gravar_controle persistiu o run_key", rk == J.run_key_atual(conn),
      f"gravado={rk}")

r2 = J.rodar()
print("   ->", r2)
checa("2a execucao: NAO e recarga (run_key ja batia)", r2["recarga"] is False)
checa("2a execucao: idempotente, mesmas linhas", r2["linhas"] == r1["linhas"])

with conn.cursor() as cur:
    cur.execute("UPDATE socio.job_controle SET valor='1999-01' WHERE chave='ultimo_run_key'")
r3 = J.rodar()
print("   ->", r3)
checa("run_key diferente dispara recarga", r3["recarga"] is True)

print()
print("=" * 66)
print("4. adiamento — rodar() desiste se a carga estiver rodando")
print("=" * 66)
J.carga_rf_em_andamento = lambda c: True
r4 = J.rodar()
print("   ->", r4)
checa("status adiado", r4["status"] == "adiado")
checa("motivo declarado", r4.get("motivo") == "carga_rf_em_andamento")

print()
print("=" * 66)
print("5. falha de lote — nao derruba a execucao e NAO grava run_key")
print("=" * 66)
J.carga_rf_em_andamento = lambda c: False
with conn.cursor() as cur:
    cur.execute("UPDATE socio.job_controle SET valor='1999-01' WHERE chave='ultimo_run_key'")

chamadas = {"n": 0}
_orig = J.processar_lote
def lote_que_falha(c, lote, dias):
    chamadas["n"] += 1
    if chamadas["n"] == 1:
        raise RuntimeError("falha simulada no 1o lote")
    return _orig(c, lote, dias)
J.processar_lote = lote_que_falha

r5 = J.rodar()
print("   ->", r5)
checa("status parcial", r5["status"] == "parcial")
checa("contou o lote com falha", r5["lotes_com_falha"] == 1)
checa("os outros lotes seguiram", r5["linhas"] > 0)
checa("NAO gravou run_key numa execucao suja",
      J.ler_controle(conn, "ultimo_run_key") == "1999-01")
J.processar_lote = _orig

print()
print("=" * 66)
print("6. sincronizar_base — substitui, nao acumula")
print("=" * 66)
n = J.sincronizar_base(conn, ["11111111000111", "22222222000122"])
checa("espelhou os 2 CNPJs", n == 2)
n = J.sincronizar_base(conn, ["33333333000133"])
checa("substituiu (nao somou): sobrou 1", n == 1,
      "merge deixaria 3 — lead que saiu da carteira tem que sair da contagem")
with conn.cursor() as cur:
    cur.execute("SELECT cnpj, cnpj_basico FROM socio.base_cnpj")
    linha = cur.fetchone()
checa("cnpj_basico preenchido com os 8 primeiros digitos",
      linha[1].strip() == linha[0].strip()[:8])

print()
print("=" * 66)
print("7. recontar_irmas_na_base — cruza por 8 digitos, nao por CNPJ completo")
print("=" * 66)
# O caso que importa: a irma gravada e MATRIZ, e a carteira tem a FILIAL dela.
with conn.cursor() as cur:
    cur.execute(
        "INSERT INTO socio.socio_empresas "
        "(cnpj, n_empresas_dono, cnpjs_irmas, n_socios, n_socios_pf, atualizado_em) "
        "VALUES ('99999999000199', 2, '88888888000100', 1, 1, NOW()) "
        "ON CONFLICT (cnpj) DO UPDATE SET cnpjs_irmas = EXCLUDED.cnpjs_irmas, "
        "n_empresas_dono = EXCLUDED.n_empresas_dono"
    )
J.sincronizar_base(conn, ["88888888000288"])   # filial 0288; a irma e a matriz 0100
J.recontar_irmas_na_base(conn)
with conn.cursor() as cur:
    cur.execute("SELECT irmas_na_base FROM socio.socio_empresas WHERE cnpj='99999999000199'")
    v = cur.fetchone()[0]
checa("acha a irma mesmo a carteira tendo so a filial", v == 1,
      f"irmas_na_base={v} — se der 0, o cruzamento voltou a ser por CNPJ completo")

J.sincronizar_base(conn, ["77777777000177"])
J.recontar_irmas_na_base(conn)
with conn.cursor() as cur:
    cur.execute("SELECT irmas_na_base FROM socio.socio_empresas WHERE cnpj='99999999000199'")
    v = cur.fetchone()[0]
checa("carteira sem a irma -> zero, nao nulo", v == 0, f"irmas_na_base={v}")

with conn.cursor() as cur:
    cur.execute("DELETE FROM socio.socio_empresas WHERE cnpj='99999999000199'")

print()
print("=" * 66)
print("8. deve_rodar — janela horaria")
print("=" * 66)
from datetime import datetime, date
for nome, dt, h, ult, esp in [
    ("03h, nunca rodou",   datetime(2026, 8, 25, 3, 0),  4, None,             False),
    ("04h, nunca rodou",   datetime(2026, 8, 25, 4, 0),  4, None,             True),
    ("05h, ja rodou hoje", datetime(2026, 8, 25, 5, 0),  4, date(2026, 8, 25), False),
    ("05h, rodou ontem",   datetime(2026, 8, 25, 5, 0),  4, date(2026, 8, 24), True),
]:
    checa(nome, J.deve_rodar(dt, h, ult) is esp)

print()
print("=" * 66)
print("limpeza")
print("=" * 66)
with conn.cursor() as cur:
    cur.execute("DROP SCHEMA IF EXISTS socio_teste CASCADE")
    # 11222333000181 e CNPJ de teste, nao e da carteira
    cur.execute("DELETE FROM socio.socio_empresas WHERE cnpj='11222333000181'")
    cur.execute("DELETE FROM socio.socio_empresas_fetch_log WHERE cnpj='11222333000181'")
    cur.execute("DELETE FROM socio.job_controle WHERE chave='ultimo_run_key'")
    cur.execute("TRUNCATE socio.base_cnpj")
    cur.execute("INSERT INTO socio.base_cnpj SELECT * FROM socio.base_cnpj_bkp_teste")
    cur.execute("DROP TABLE socio.base_cnpj_bkp_teste")
    cur.execute("SELECT count(*) FROM socio.base_cnpj")
    base_voltou = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM socio.socio_empresas")
    total = cur.fetchone()[0]
checa("schema de teste removido e carteira intacta (22141)", total == 22141,
      f"total={total}")
checa("base_cnpj restaurada ao estado original", base_voltou == BASE_ORIGINAL,
      f"voltou={base_voltou}, original={BASE_ORIGINAL}")

# irmas_na_base ficou zerado pelos testes acima (rodaram com carteira falsa).
# Recontar contra a base_cnpj ja restaurada devolve o numero de verdade.
at, teto = J.recontar_irmas_na_base(conn)
with conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM socio.socio_empresas WHERE irmas_na_base > 0")
    com_irma = cur.fetchone()[0]
checa("irmas_na_base reconciliado com a carteira real", com_irma > 0,
      f"{com_irma} linha(s) com irma na carteira")

print()
print("=" * 66)
if FALHAS:
    print(f"{len(FALHAS)} FALHA(S): " + ", ".join(FALHAS))
    sys.exit(1)
print("TODOS OS CENARIOS PASSARAM")
