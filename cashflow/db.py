"""PostgreSQL: uma conexao por mensagem (sessao) e uma transacao por operacao."""
import hashlib
import threading
import uuid
from contextlib import contextmanager
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from cashflow import config
from cashflow.util import dividir_centavos, hoje, somar_meses, ultimo_dia

_local = threading.local()
_tabelas_prontas = False
_tabelas_lock = threading.Lock()

# Tudo aditivo: tabelas e colunas novas, nada e apagado ou alterado nos dados existentes.
SCHEMA = [
    # Evita dois workers criando as mesmas tabelas ao mesmo tempo num banco novo.
    "SELECT pg_advisory_xact_lock(727274)",
    """
    CREATE TABLE IF NOT EXISTS transacoes (
        id SERIAL PRIMARY KEY,
        telefone TEXT NOT NULL,
        descricao TEXT NOT NULL,
        valor NUMERIC(12,2) NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('entrada','saida')),
        categoria TEXT NOT NULL DEFAULT 'Outros',
        data DATE NOT NULL DEFAULT CURRENT_DATE,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE transacoes ADD COLUMN IF NOT EXISTS grupo TEXT",
    "ALTER TABLE transacoes ADD COLUMN IF NOT EXISTS recorrente_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_transacoes_tel_data ON transacoes (telefone, data)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_transacoes_recorrente
    ON transacoes (recorrente_id, data) WHERE recorrente_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS limites (
        telefone TEXT PRIMARY KEY,
        limite_mensal NUMERIC(12,2) NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS limites_categoria (
        telefone TEXT NOT NULL,
        categoria TEXT NOT NULL,
        valor NUMERIC(12,2) NOT NULL,
        PRIMARY KEY (telefone, categoria)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metas (
        id SERIAL PRIMARY KEY,
        telefone TEXT NOT NULL,
        nome TEXT NOT NULL,
        alvo NUMERIC(12,2) NOT NULL,
        guardado NUMERIC(12,2) NOT NULL DEFAULT 0,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_metas_nome ON metas (telefone, lower(nome))",
    """
    CREATE TABLE IF NOT EXISTS recorrentes (
        id SERIAL PRIMARY KEY,
        telefone TEXT NOT NULL,
        descricao TEXT NOT NULL,
        valor NUMERIC(12,2) NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('entrada','saida')),
        categoria TEXT NOT NULL DEFAULT 'Outros',
        dia SMALLINT NOT NULL CHECK (dia BETWEEN 1 AND 31),
        inicio DATE NOT NULL,
        gerado_ate DATE,
        lembrado_em DATE,
        ativo BOOLEAN NOT NULL DEFAULT TRUE,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recorrentes_tel ON recorrentes (telefone) WHERE ativo",
    """
    CREATE TABLE IF NOT EXISTS pendencias (
        telefone TEXT PRIMARY KEY,
        acao TEXT NOT NULL,
        dados JSONB NOT NULL DEFAULT '{}',
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mensagens_processadas (
        id TEXT PRIMARY KEY,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        telefone TEXT PRIMARY KEY,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        lembretes BOOLEAN NOT NULL DEFAULT TRUE,
        aviso_assinatura_em TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assinaturas (
        chave TEXT PRIMARY KEY,
        ativa BOOLEAN NOT NULL,
        origem TEXT,
        atualizada_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


def criar_tabelas(conn):
    with conn.cursor() as cur:
        # Banco ja atualizado: nao roda DDL nenhum (ALTER TABLE trava a tabela, mesmo sem mudar nada).
        # Ao acrescentar algo no SCHEMA, inclua o objeto novo nesta checagem.
        cur.execute("""
            SELECT to_regclass('public.assinaturas') IS NOT NULL
               AND to_regclass('public.idx_transacoes_recorrente') IS NOT NULL AS ok
        """)
        if not cur.fetchone()["ok"]:
            # Se outra consulta longa estiver segurando a tabela, desiste em 10s em vez de travar o bot.
            cur.execute("SET LOCAL lock_timeout = '10s'")
            for sql in SCHEMA:
                cur.execute(sql)
    conn.commit()


def conectar():
    global _tabelas_prontas

    conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)

    # Cria/atualiza as tabelas uma vez por processo, e nao a cada conexao.
    if not _tabelas_prontas:
        with _tabelas_lock:
            if not _tabelas_prontas:
                try:
                    criar_tabelas(conn)
                except Exception:
                    conn.close()
                    raise
                _tabelas_prontas = True

    return conn


@contextmanager
def sessao():
    # Uma conexao para a mensagem inteira, reaproveitada por todas as consultas.
    if getattr(_local, "conn", None) is not None:
        yield
        return

    _local.conn = conectar()
    try:
        yield
    finally:
        conn, _local.conn = _local.conn, None
        conn.close()


@contextmanager
def transacao():
    conn = getattr(_local, "conn", None)
    propria = conn is None

    if propria:
        conn = conectar()
    elif conn.closed:
        conn = _local.conn = conectar()

    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        if propria:
            conn.close()


# ─── Lancamentos ──────────────────────────────────────────────────────────────

# Se mudar id_curto(), mude tambem o left(md5(id::text), 6) de _por_id_curto().
def id_curto(pk: int) -> str:
    return hashlib.md5(str(pk).encode()).hexdigest()[:6]


COLUNAS = "id, descricao, valor, tipo, categoria, data, grupo, recorrente_id"


def _alvo(cur, telefone, short_id=None, usar_ultimo=False):
    if usar_ultimo:
        cur.execute(
            "SELECT {} FROM transacoes WHERE telefone=%s ORDER BY id DESC LIMIT 1".format(COLUNAS),
            (telefone,)
        )
    else:
        cur.execute(
            "SELECT {} FROM transacoes WHERE telefone=%s AND left(md5(id::text), 6)=%s ORDER BY id DESC LIMIT 1".format(COLUNAS),
            (telefone, (short_id or "").lower())
        )
    row = cur.fetchone()
    return dict(row) if row else None


def salvar_transacao(telefone, descricao, valor, tipo, categoria, data=None, grupo=None, recorrente_id=None):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO transacoes (telefone, descricao, valor, tipo, categoria, data, grupo, recorrente_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (telefone, descricao, valor, tipo, categoria, data or hoje(), grupo, recorrente_id)
        )
        pk = cur.fetchone()["id"]
    return pk, id_curto(pk)


def salvar_parcelas(telefone, descricao, tipo, categoria, n, total=None, valor_parcela=None, data=None):
    # "TV (1/10)", "TV (2/10)"... um por mes, todos com o mesmo grupo.
    inicio = data or hoje()
    valores = [valor_parcela] * n if valor_parcela else dividir_centavos(total, n)
    grupo = uuid.uuid4().hex[:12]
    parcelas = []

    with transacao() as cur:
        for i, valor in enumerate(valores):
            d = somar_meses(inicio, i)
            cur.execute(
                """
                INSERT INTO transacoes (telefone, descricao, valor, tipo, categoria, data, grupo)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (telefone, "{} ({}/{})".format(descricao, i + 1, n), valor, tipo, categoria, d, grupo)
            )
            parcelas.append({"id": cur.fetchone()["id"], "data": d, "valor": valor})

    return parcelas


def editar_transacao(telefone, short_id=None, usar_ultimo=False, novo_valor=None, nova_categoria=None):
    # Retorna o lancamento atualizado, None se nao achou, ou {"erro": "parcelado"}.
    with transacao() as cur:
        row = _alvo(cur, telefone, short_id, usar_ultimo)

        if not row:
            return None

        if novo_valor and row["grupo"]:
            return {"erro": "parcelado", **row}

        if novo_valor and novo_valor > 0:
            cur.execute("UPDATE transacoes SET valor=%s WHERE id=%s", (novo_valor, row["id"]))

        if nova_categoria:
            # Numa compra parcelada, a categoria muda em todas as parcelas.
            if row["grupo"]:
                cur.execute(
                    "UPDATE transacoes SET categoria=%s WHERE telefone=%s AND grupo=%s",
                    (nova_categoria, telefone, row["grupo"])
                )
            else:
                cur.execute("UPDATE transacoes SET categoria=%s WHERE id=%s", (nova_categoria, row["id"]))

        cur.execute("SELECT {} FROM transacoes WHERE id=%s".format(COLUNAS), (row["id"],))
        return dict(cur.fetchone())


def apagar_transacao(telefone, short_id=None, usar_ultimo=False):
    # Apaga o lancamento; se for parcela, apaga a compra parcelada inteira.
    with transacao() as cur:
        row = _alvo(cur, telefone, short_id, usar_ultimo)

        if not row:
            return None

        if row["grupo"]:
            cur.execute(
                "DELETE FROM transacoes WHERE telefone=%s AND grupo=%s RETURNING valor",
                (telefone, row["grupo"])
            )
            apagadas = cur.fetchall()
            row["parcelas"] = len(apagadas)
            row["total"] = sum(float(r["valor"]) for r in apagadas)
        else:
            cur.execute("DELETE FROM transacoes WHERE id=%s", (row["id"],))

    return row


def buscar_transacoes(telefone, data_ini, data_fim):
    with transacao() as cur:
        cur.execute(
            """
            SELECT {}
            FROM transacoes
            WHERE telefone=%s AND data BETWEEN %s AND %s
            ORDER BY data, id
            """.format(COLUNAS),
            (telefone, data_ini, data_fim)
        )
        return [dict(r) for r in cur.fetchall()]


def todas_transacoes(telefone):
    with transacao() as cur:
        cur.execute(
            "SELECT {} FROM transacoes WHERE telefone=%s ORDER BY data, id".format(COLUNAS),
            (telefone,)
        )
        return [dict(r) for r in cur.fetchall()]


def totais_por_mes(telefone, data_ini, data_fim):
    with transacao() as cur:
        cur.execute(
            """
            SELECT date_trunc('month', data)::date AS mes, tipo, SUM(valor) AS total, COUNT(*) AS qtd
            FROM transacoes
            WHERE telefone=%s AND data BETWEEN %s AND %s
            GROUP BY 1, 2
            ORDER BY 1
            """,
            (telefone, data_ini, data_fim)
        )
        return [dict(r) for r in cur.fetchall()]


def tem_lancamentos(telefone) -> bool:
    with transacao() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM transacoes WHERE telefone=%s) AS tem", (telefone,))
        return cur.fetchone()["tem"]


# ─── Limites ──────────────────────────────────────────────────────────────────

def buscar_limite(telefone):
    with transacao() as cur:
        cur.execute("SELECT limite_mensal FROM limites WHERE telefone=%s", (telefone,))
        row = cur.fetchone()
        return float(row["limite_mensal"]) if row else 0.0


def salvar_limite(telefone, valor):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO limites (telefone, limite_mensal)
            VALUES (%s, %s)
            ON CONFLICT (telefone)
            DO UPDATE SET limite_mensal=EXCLUDED.limite_mensal
            """,
            (telefone, valor)
        )


def remover_limite(telefone) -> bool:
    with transacao() as cur:
        cur.execute("DELETE FROM limites WHERE telefone=%s", (telefone,))
        return cur.rowcount > 0


def limites_categoria(telefone) -> dict:
    with transacao() as cur:
        cur.execute("SELECT categoria, valor FROM limites_categoria WHERE telefone=%s ORDER BY categoria", (telefone,))
        return {r["categoria"]: float(r["valor"]) for r in cur.fetchall()}


def salvar_limite_categoria(telefone, categoria, valor):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO limites_categoria (telefone, categoria, valor)
            VALUES (%s, %s, %s)
            ON CONFLICT (telefone, categoria)
            DO UPDATE SET valor=EXCLUDED.valor
            """,
            (telefone, categoria, valor)
        )


def remover_limite_categoria(telefone, categoria) -> bool:
    with transacao() as cur:
        cur.execute("DELETE FROM limites_categoria WHERE telefone=%s AND categoria=%s", (telefone, categoria))
        return cur.rowcount > 0


# ─── Metas ────────────────────────────────────────────────────────────────────

def listar_metas(telefone):
    with transacao() as cur:
        cur.execute("SELECT * FROM metas WHERE telefone=%s ORDER BY criado_em, id", (telefone,))
        return [dict(r) for r in cur.fetchall()]


def salvar_meta(telefone, nome, alvo):
    # Cria a meta ou atualiza o valor alvo se ja existir uma com o mesmo nome.
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO metas (telefone, nome, alvo)
            VALUES (%s, %s, %s)
            ON CONFLICT (telefone, lower(nome))
            DO UPDATE SET alvo=EXCLUDED.alvo
            RETURNING *, (xmax = 0) AS criada
            """,
            (telefone, nome, alvo)
        )
        return dict(cur.fetchone())


def movimentar_meta(meta_id, delta):
    with transacao() as cur:
        cur.execute(
            "UPDATE metas SET guardado=GREATEST(guardado + %s, 0) WHERE id=%s RETURNING *",
            (delta, meta_id)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def apagar_meta(meta_id):
    with transacao() as cur:
        cur.execute("DELETE FROM metas WHERE id=%s", (meta_id,))


# ─── Contas fixas (recorrentes) ───────────────────────────────────────────────

def criar_recorrente(telefone, descricao, valor, tipo, categoria, dia, inicio):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO recorrentes (telefone, descricao, valor, tipo, categoria, dia, inicio)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (telefone, descricao, valor, tipo, categoria, dia, inicio)
        )
        return dict(cur.fetchone())


def listar_recorrentes(telefone):
    with transacao() as cur:
        cur.execute("SELECT * FROM recorrentes WHERE telefone=%s AND ativo ORDER BY dia, id", (telefone,))
        return [dict(r) for r in cur.fetchall()]


def atualizar_recorrente(rec_id, valor, tipo, categoria, dia):
    with transacao() as cur:
        cur.execute(
            "UPDATE recorrentes SET valor=%s, tipo=%s, categoria=%s, dia=%s WHERE id=%s RETURNING *",
            (valor, tipo, categoria, dia, rec_id)
        )
        return dict(cur.fetchone())


def desativar_recorrente(rec_id):
    with transacao() as cur:
        cur.execute("UPDATE recorrentes SET ativo=FALSE WHERE id=%s", (rec_id,))


def ocorrencia(dia: int, ano: int, mes: int) -> date:
    # "Todo dia 31" em fevereiro cai no dia 28 (ou 29).
    return date(ano, mes, min(dia, ultimo_dia(ano, mes)))


def gerar_recorrentes(telefone, ate=None):
    # Lanca as contas fixas que venceram desde a ultima vez (inclusive meses sem o usuario aparecer).
    ate = ate or hoje()
    gerados = []

    with transacao() as cur:
        cur.execute(
            """
            SELECT * FROM recorrentes
            WHERE telefone=%s AND ativo AND (gerado_ate IS NULL OR gerado_ate < %s)
            FOR UPDATE
            """,
            (telefone, ate)
        )

        for rec in cur.fetchall():
            desde = rec["gerado_ate"] + timedelta(days=1) if rec["gerado_ate"] else rec["inicio"]
            mes = desde.replace(day=1)

            while mes <= ate:
                d = ocorrencia(rec["dia"], mes.year, mes.month)

                if desde <= d <= ate:
                    cur.execute(
                        """
                        INSERT INTO transacoes (telefone, descricao, valor, tipo, categoria, data, recorrente_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (telefone, rec["descricao"], rec["valor"], rec["tipo"], rec["categoria"], d, rec["id"])
                    )
                    if cur.fetchone():
                        gerados.append({"descricao": rec["descricao"], "valor": float(rec["valor"]),
                                        "tipo": rec["tipo"], "data": d})

                mes = somar_meses(mes, 1)

            cur.execute("UPDATE recorrentes SET gerado_ate=%s WHERE id=%s", (ate, rec["id"]))

    return gerados


def telefones_com_recorrentes():
    with transacao() as cur:
        cur.execute("SELECT DISTINCT telefone FROM recorrentes WHERE ativo")
        return [r["telefone"] for r in cur.fetchall()]


def recorrentes_para_lembrar(dia_alvo: date):
    # Contas (saida) que vencem em dia_alvo, de quem nao desligou os lembretes, ainda nao lembradas hoje.
    with transacao() as cur:
        cur.execute(
            """
            SELECT r.*
            FROM recorrentes r
            LEFT JOIN usuarios u ON u.telefone = r.telefone
            WHERE r.ativo AND r.tipo='saida' AND COALESCE(u.lembretes, TRUE)
              AND (r.lembrado_em IS NULL OR r.lembrado_em < %s)
              AND r.inicio <= %s
            """,
            (hoje(), dia_alvo)
        )
        return [dict(r) for r in cur.fetchall()
                if ocorrencia(r["dia"], dia_alvo.year, dia_alvo.month) == dia_alvo]


def marcar_lembrado(rec_id):
    with transacao() as cur:
        cur.execute("UPDATE recorrentes SET lembrado_em=%s WHERE id=%s", (hoje(), rec_id))


# ─── Conversa: pergunta em aberto ("Qual o novo valor?") ──────────────────────

def definir_pendencia(telefone, acao, dados=None):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO pendencias (telefone, acao, dados, criado_em)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (telefone)
            DO UPDATE SET acao=EXCLUDED.acao, dados=EXCLUDED.dados, criado_em=NOW()
            """,
            (telefone, acao, psycopg2.extras.Json(dados or {}))
        )


def obter_pendencia(telefone, minutos=10):
    with transacao() as cur:
        cur.execute(
            """
            SELECT acao, dados FROM pendencias
            WHERE telefone=%s AND criado_em > NOW() - make_interval(mins => %s)
            """,
            (telefone, minutos)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def limpar_pendencia(telefone):
    with transacao() as cur:
        cur.execute("DELETE FROM pendencias WHERE telefone=%s", (telefone,))


# ─── Mensagens ja processadas (a Evolution pode reenviar o mesmo webhook) ────

def marcar_processada(chave) -> bool:
    # True se e a primeira vez que essa mensagem chega.
    with transacao() as cur:
        cur.execute(
            "INSERT INTO mensagens_processadas (id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id",
            (chave,)
        )
        return cur.fetchone() is not None


def limpar_processadas(dias=3):
    with transacao() as cur:
        cur.execute("DELETE FROM mensagens_processadas WHERE criado_em < NOW() - make_interval(days => %s)", (dias,))


# ─── Usuarios e assinatura ────────────────────────────────────────────────────

def registrar_usuario(telefone):
    # Retorna o usuario; "novo" = primeira mensagem que ele manda (desde que esta tabela existe).
    with transacao() as cur:
        cur.execute(
            "INSERT INTO usuarios (telefone) VALUES (%s) ON CONFLICT DO NOTHING RETURNING telefone",
            (telefone,)
        )
        novo = cur.fetchone() is not None
        cur.execute("SELECT * FROM usuarios WHERE telefone=%s", (telefone,))
        usuario = dict(cur.fetchone())

    usuario["novo"] = novo
    return usuario


def definir_lembretes(telefone, ligado):
    with transacao() as cur:
        cur.execute("UPDATE usuarios SET lembretes=%s WHERE telefone=%s", (ligado, telefone))


def marcar_aviso_assinatura(telefone):
    with transacao() as cur:
        cur.execute("UPDATE usuarios SET aviso_assinatura_em=NOW() WHERE telefone=%s", (telefone,))


def definir_assinatura(chave, ativa, origem):
    with transacao() as cur:
        cur.execute(
            """
            INSERT INTO assinaturas (chave, ativa, origem, atualizada_em)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (chave)
            DO UPDATE SET ativa=EXCLUDED.ativa, origem=EXCLUDED.origem, atualizada_em=NOW()
            """,
            (chave, ativa, origem)
        )


def assinatura_ativa(chave) -> bool:
    with transacao() as cur:
        cur.execute("SELECT ativa FROM assinaturas WHERE chave=%s", (chave,))
        row = cur.fetchone()
        return bool(row and row["ativa"])


# ─── LGPD ─────────────────────────────────────────────────────────────────────

def apagar_dados(telefone) -> int:
    # Apaga tudo do usuario (a assinatura paga fica, ela e do pagamento, nao do uso).
    with transacao() as cur:
        cur.execute("DELETE FROM transacoes WHERE telefone=%s", (telefone,))
        apagados = cur.rowcount
        for tabela in ("limites", "limites_categoria", "metas", "recorrentes", "pendencias", "usuarios"):
            cur.execute("DELETE FROM {} WHERE telefone=%s".format(tabela), (telefone,))
    return apagados
