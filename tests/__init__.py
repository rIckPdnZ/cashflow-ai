"""Testes do Cash Flow IA.

    python -m unittest -v                         # testes rapidos, sem banco
    TEST_DATABASE_URL=postgresql://... python -m unittest -v   # + conversas completas num banco de TESTE
"""
import os

# O app le as variaveis na importacao; os testes nunca falam com Groq/Evolution de verdade.
os.environ.setdefault("GROQ_API_KEY", "teste")
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL") or "postgresql://teste@localhost/teste")
os.environ.setdefault("EVOLUTION_URL", "http://evolution.teste")
os.environ.setdefault("EVOLUTION_KEY", "teste")
os.environ.setdefault("EVOLUTION_INSTANCE", "teste")
